# Copyright 2023 The Qwen team, Alibaba Group. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import atexit
import base64
import io
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import socket

from pathlib import Path
from typing import Dict, List, Optional, Union

import json5

import jsonschema

from agent_cascade.log import logger
from agent_cascade.tools.base import BaseToolWithFileAccess, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.tool_utils import (
    MAX_SPILL_SIZE,  # Consistent 50MB limit across all modules
    generate_spillover_filename,  # Shared collision detection helper
)
from agent_cascade.utils.utils import append_signal_handler, extract_code, has_chinese_chars, json_loads, print_traceback
from agent_cascade.utils.code_path_resolver import resolve_code_paths, build_path_resolution_notice, set_active_mappings


# --- Timeout Configuration ---
# Per-execution timeout: max seconds a single code cell can run before being killed.
# Can be overridden by env var or tool config.
CODE_EXECUTION_TIMEOUT = int(os.getenv('M6_CODE_INTERPRETER_EXEC_TIMEOUT', '120'))

# Container watchdog timeout: if the kernel becomes completely unresponsive
# for this many seconds, kill and restart the container.
CONTAINER_WATCHDOG_TIMEOUT = int(os.getenv('M6_CODE_INTERPRETER_WATCHDOG_TIMEOUT', '300'))

# Resource limits for Docker containers (configurable via env vars)
CONTAINER_MEMORY_LIMIT = os.getenv('M6_CODE_INTERPRETER_CONTAINER_MEMORY', '2g')
CONTAINER_CPU_LIMIT = float(os.getenv('M6_CODE_INTERPRETER_CONTAINER_CPUS', '2.0'))
CONTAINER_PID_LIMIT = int(os.getenv('M6_CODE_INTERPRETER_CONTAINER_PIDS', '100'))

# Maximum size for spillover files (50MB) - uses MAX_SPILL_SIZE from tool_utils directly
# This prevents disk exhaustion from massive code interpreter outputs

LAUNCH_KERNEL_PY = """
from ipykernel import kernelapp as app
app.launch_new_instance()
"""

INIT_CODE_FILE = str(Path(__file__).absolute().parent / 'resource' / 'code_interpreter_init_kernel.py')
ALIB_FONT_FILE = str(Path(__file__).absolute().parent / 'resource' / 'AlibabaPuHuiTi-3-45-Light.ttf')
DOCKER_IMAGE_FILE = str(Path(__file__).absolute().parent / 'resource' / 'code_interpreter_image.dockerfile')

_KERNEL_CLIENTS: dict = {}
_DOCKER_CONTAINERS: Dict[str, str] = {}

# Track last activity per kernel for watchdog (value is {'last_active': float, 'work_dir': str})
_KERNEL_ACTIVITY: Dict[str, dict] = {}

# Thread-safe lock for mutating shared kernel state (_KERNEL_CLIENTS, _DOCKER_CONTAINERS, _KERNEL_ACTIVITY)
_KERNEL_LOCK = threading.Lock()

# Track kernels killed by the watchdog so that in-flight calls can detect it and return a proper error
_WATCHDOG_KILLED: set = set()

# Track "stale" containers after Tier 3 kill for warm restart.
# Value format: {'container_id': str, 'timestamp': float} — timestamp enables TTL cleanup.
_STALE_CONTAINERS: Dict[str, dict] = {}

# TTL for stale containers in seconds (10 minutes)
STALE_CONTAINER_TTL = int(os.getenv('M6_CODE_INTERPRETER_STALE_TTL', '600'))


def _check_container_healthy(container_id: str) -> bool:
    """Check if a Docker container exists and is running via docker inspect.

    Returns True if the container is in 'running' state, False otherwise.
    """
    try:
        result = subprocess.run(
            ['docker', 'inspect', '--format', '{{.State.Status}}', container_id],
            timeout=5, capture_output=True, text=True, encoding='utf-8', errors='replace'
        )
        return result.stdout.strip() == 'running'
    except Exception as e:
        logger.debug(f"Container health check failed for {container_id}: {e}")
        return False


def _find_running_container(container_name: str) -> Optional[str]:
    """Find a running container by name and return its ID.

    Returns the container ID string if found and running, None otherwise.
    """
    try:
        result = subprocess.run(
            ['docker', 'ps', '-q', '--filter', f'name={container_name}', '--filter', 'status=running'],
            timeout=5, capture_output=True, text=True, encoding='utf-8', errors='replace'
        )
        cid = result.stdout.strip()
        return cid if cid else None
    except Exception as e:
        logger.debug(f"Failed to find running container '{container_name}': {e}")
        return None


def _kill_kernels_and_containers(_sig_num=None, _frame=None):
    # Stop the watchdog thread first
    if '_WATCHDOG_THREAD' in globals() and _WATCHDOG_THREAD.is_alive():
        _WATCHDOG_TERMINATE.set()
        _WATCHDOG_THREAD.join(timeout=5)

    with _KERNEL_LOCK:
        for v in _KERNEL_CLIENTS.values():
            v.shutdown()
        for k in list(_KERNEL_CLIENTS.keys()):
            del _KERNEL_CLIENTS[k]

        for container_id in _DOCKER_CONTAINERS.values():
            try:
                subprocess.run(['docker', 'stop', container_id], timeout=10, capture_output=True, encoding='utf-8', errors='replace')
                subprocess.run(['docker', 'rm', container_id], timeout=10, capture_output=True, encoding='utf-8', errors='replace')
            except Exception:
                print(f"WARNING: Failed to stop and remove the Docker container: {container_id}")
        for k in list(_DOCKER_CONTAINERS.keys()):
            del _DOCKER_CONTAINERS[k]

        _KERNEL_ACTIVITY.clear()
        _WATCHDOG_KILLED.clear()
        _STALE_CONTAINERS.clear()


# Make sure all containers are terminated even if killed abnormally:
# If not running in the main thread, (for example run in streamlit)
# register a signal would cause a RuntimeError
if threading.current_thread() is threading.main_thread():
    atexit.register(_kill_kernels_and_containers)
    append_signal_handler(signal.SIGTERM, _kill_kernels_and_containers)
    append_signal_handler(signal.SIGINT, _kill_kernels_and_containers)

# --- Watchdog Thread: Monitors kernel responsiveness ---
_WATCHDOG_TERMINATE = threading.Event()

def _kernel_watchdog():
    """Background thread that kills unresponsive kernels.
    
    Checks every 5 seconds if any kernel has been inactive for more than
    CONTAINER_WATCHDOG_TIMEOUT. If so, it stops and removes the container,
    cleans up the client, and logs a warning.
    """
    while not _WATCHDOG_TERMINATE.is_set():
        _WATCHDOG_TERMINATE.wait(timeout=5)
        now = time.time()
        stale_kernels = []
        with _KERNEL_LOCK:
            for kernel_id, activity in list(_KERNEL_ACTIVITY.items()):
                if now - activity['last_active'] > CONTAINER_WATCHDOG_TIMEOUT:
                    stale_kernels.append(kernel_id)
        
        for kernel_id in stale_kernels:
            logger.warning(
                f"Code interpreter watchdog: Kernel {kernel_id} inactive for "
                f"{CONTAINER_WATCHDOG_TIMEOUT}s. Killing container."
            )
            
            # Mark as watchdog-killed and remove from client/container tracking atomically
            kc_to_shutdown = None
            container_id_to_kill = None
            with _KERNEL_LOCK:
                _WATCHDOG_KILLED.add(kernel_id)
                
                # Kill the kernel client
                if kernel_id in _KERNEL_CLIENTS:
                    try:
                        _KERNEL_CLIENTS[kernel_id].shutdown()
                    except Exception:
                        pass
                    kc_to_shutdown = None  # already shut down
                    del _KERNEL_CLIENTS[kernel_id]
                
                # Record container for killing (docker ops are slow, do outside lock)
                if kernel_id in _DOCKER_CONTAINERS:
                    container_id_to_kill = _DOCKER_CONTAINERS[kernel_id]
                    del _DOCKER_CONTAINERS[kernel_id]
                
                # Clean up activity tracking
                work_dir_base = _KERNEL_ACTIVITY.get(kernel_id, {}).get('work_dir', '.')
                if kernel_id in _KERNEL_ACTIVITY:
                    del _KERNEL_ACTIVITY[kernel_id]
            
            # Kill the container outside lock (docker ops can be slow)
            if container_id_to_kill is not None:
                try:
                    subprocess.run(
                        ['docker', 'stop', container_id_to_kill], timeout=10,
                        capture_output=True, encoding='utf-8', errors='replace'
                    )
                    subprocess.run(
                        ['docker', 'rm', container_id_to_kill], timeout=10,
                        capture_output=True, encoding='utf-8', errors='replace'
                    )
                except Exception as e:
                    logger.warning(f"Failed to clean up stale container {container_id_to_kill}: {e}")
            
            # Clean up connection files, launch script, and path mapping — use the work_dir stored at kernel start
            for suffix in ['_host.json', '_container.json']:
                conn_file = os.path.join(work_dir_base, f'kernel_connection_file_{kernel_id}{suffix}')
                try:
                    if os.path.exists(conn_file):
                        os.remove(conn_file)
                except OSError as e:
                    logger.warning(f"Failed to remove connection file {conn_file}: {e}")
            
            launch_script = os.path.join(work_dir_base, f'launch_kernel_{kernel_id}.py')
            try:
                if os.path.exists(launch_script):
                    os.remove(launch_script)
            except OSError as e:
                logger.warning(f"Failed to remove launch script {launch_script}: {e}")
            
            # Clean up path mapping file for this kernel
            mapping_file = os.path.join(work_dir_base, f'path_mapping_{kernel_id}.json')
            try:
                if os.path.exists(mapping_file):
                    os.remove(mapping_file)
            except OSError as e:
                logger.warning(f"Failed to remove path mapping file {mapping_file}: {e}")

        _cleanup_stale_containers(now)


def _cleanup_stale_containers(now: float):
    """Remove stale containers older than STALE_CONTAINER_TTL.

    Keeps the stale container registry from accumulating entries indefinitely.
    Called from the watchdog loop on each tick.

    Lock strategy: scan under lock, docker rm outside lock, then batch-delete
    from the dict under a single lock acquisition to minimize hold time.
    """
    expired_stale = []
    with _KERNEL_LOCK:
        for kernel_id, info in list(_STALE_CONTAINERS.items()):
            age = now - info.get('timestamp', 0)
            if age > STALE_CONTAINER_TTL:
                expired_stale.append((kernel_id, info['container_id']))

    # Remove containers outside the lock (docker ops can be slow)
    removed_ids = set()
    for kernel_id, cid in expired_stale:
        try:
            subprocess.run(
                ['docker', 'rm', '-f', cid], timeout=10,
                capture_output=True, encoding='utf-8', errors='replace'
            )
            removed_ids.add(kernel_id)
        except Exception as e:
            logger.debug(f"Failed to remove expired stale container {cid}: {e}")

    # Batch-delete from dict under a single lock acquisition
    if removed_ids:
        with _KERNEL_LOCK:
            for kernel_id in removed_ids:
                _STALE_CONTAINERS.pop(kernel_id, None)


_WATCHDOG_THREAD = threading.Thread(target=_kernel_watchdog, daemon=True, name='code-interpreter-watchdog')
_WATCHDOG_THREAD.start()
logger.info(f"Code interpreter watchdog started (timeout={CONTAINER_WATCHDOG_TIMEOUT}s)")


@register_tool('code_interpreter')
class CodeInterpreter(BaseToolWithFileAccess):
    description = TOOL_METADATA['code_interpreter']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'code': {
                'description': TOOL_METADATA['code_interpreter']['parameters']['code'],
                'type': 'string',
            },
            'fix_paths': {
                'description': 'Auto-translate Windows host paths (e.g. N:\\work\\...) to Docker container paths (/workspace/...). Set to false to disable.',
                'type': 'boolean',
                'default': True,
            }
        },
        'required': ['code'],
    }

    def __init__(self, cfg: Optional[Dict] = None):
        super().__init__(cfg)
        # Priority: config > env var > inherited default — merged into single assignment
        env_work_dir = os.getenv('M6_CODE_INTERPRETER_WORK_DIR')
        self.work_dir: str = str(self.cfg.get('work_dir', env_work_dir or self.work_dir))
        # Extra work folder paths to mount into the Docker container (copied to avoid shared mutable state)
        self.extra_work_folders_ro: List[str] = list(self.cfg.get('extra_work_folders_ro', []))
        self.extra_work_folders_rw: List[str] = list(self.cfg.get('extra_work_folders_rw', []))
        # Store reference to operation_manager for dynamic extra-folder resolution at kernel start time
        self._operation_manager = None
        self.instance_id: str = str(uuid.uuid4())
        self.docker_image_name: str = 'code-interpreter:latest'
        self.container_work_dir = '/workspace'
        _check_docker_availability()
        _check_host_deps()

    @property
    def args_format(self) -> str:
        fmt = self.cfg.get('args_format')
        if fmt is None:
            if has_chinese_chars([self.name_for_human, self.name, self.description, self.parameters]):
                fmt = '此工具的输入应为Markdown代码块。'
            else:
                fmt = 'Enclose the code within triple backticks (`) at the beginning and end of the code.'
        return fmt

    def call(self, params: Union[str, dict], files: List[str] = None, timeout: Optional[int] = None, **kwargs) -> str:
        super().call(params=params, files=files)  # copy remote files to work_dir

        # Validate arguments like all other tools do — this catches empty/None/malformed args early
        # and also strips thinking block contamination from parameter values.
        # Strategy: try strict JSON validation first; if it fails, fall back to lenient parsing
        # to preserve backward compatibility with LLMs that send non-JSON input (e.g., raw code blocks).
        try:
            validated_params = self._verify_json_format_args(params)
            code = validated_params.get('code', '')
        except (ValueError, jsonschema.ValidationError):  # _verify_json_format_args can raise ValueError or jsonschema.ValidationError on failure
            # Fallback: try lenient parsing, then extract raw code blocks
            if isinstance(params, dict):
                code = params.get('code', '')
            else:
                try:
                    params_dict = json_loads(params)
                    if isinstance(params_dict, dict):
                        code = params_dict.get('code', '')
                    else:
                        code = extract_code(params)
                except (ValueError, TypeError):
                    code = extract_code(params)

        # Legacy fallback: strip markdown wrappers only if code was JSON-embedded
        # (XML-extracted code arrives clean and should not be modified)
        if isinstance(code, str) and code.strip().startswith('```'):
            code = extract_code(code)

        if not code.strip():
            return ''

        # Determine if path fixing is enabled (default: True)
        fix_paths = True  # default
        if isinstance(params, dict) and 'fix_paths' in params:
            fix_paths = bool(params.get('fix_paths', True))

        # Use configured timeout: explicit param > config > default
        exec_timeout = timeout
        if exec_timeout is None:
            exec_timeout = self.cfg.get('execution_timeout', CODE_EXECUTION_TIMEOUT)

        kernel_id: str = f'{self.instance_id}_{os.getpid()}'
        
        # Phase 1: Acquire lock, check state, and determine kernel strategy
        with _KERNEL_LOCK:
            # Check if the kernel was killed by the watchdog — clean up and start fresh (thread-safe)
            if kernel_id in _WATCHDOG_KILLED:
                logger.warning(f"Kernel {kernel_id} was killed by watchdog; starting fresh.")
                _WATCHDOG_KILLED.discard(kernel_id)
            
            # Determine whether this is a new kernel or an existing one
            needs_init = False
            if kernel_id in _KERNEL_CLIENTS:
                kc = _KERNEL_CLIENTS[kernel_id]
                container_id = _DOCKER_CONTAINERS.get(kernel_id)
            else:
                kc = None
                container_id = None

        # Phase 2: Start kernel if needed (outside lock to avoid deadlock with internal lock acquisition)
        if kc is None:
            # Try warm restart: check if a container with the same name is already running.
            warm_restart_ok = False
            found_cid = None
            container_name = f'code_interpreter_{kernel_id}'

            with _KERNEL_LOCK:
                # Check stale containers first (most likely candidate)
                stale_info = _STALE_CONTAINERS.get(kernel_id)

            if stale_info:
                stale_cid = stale_info['container_id']
                if _check_container_healthy(stale_cid):
                    logger.info(f"Warm restart: Reusing healthy container {stale_cid} for kernel {kernel_id}")
                    found_cid = stale_cid
                    warm_restart_ok = True

            # Also check for any running container with the same name (e.g., from a crash)
            if not warm_restart_ok:
                found_cid = _find_running_container(container_name)
                if found_cid:
                    logger.info(f"Warm restart: Found running container {found_cid} for kernel {kernel_id}")
                    warm_restart_ok = True

            needs_init = True
            if warm_restart_ok:
                try:
                    kc, container_id = self._warm_restart_kernel(kernel_id, found_cid)
                except RuntimeError:
                    logger.warning(f"Warm restart failed for {kernel_id}, falling back to full start")
                    kc, container_id = self._start_kernel(kernel_id)
            else:
                kc, container_id = self._start_kernel(kernel_id)

            # Register kernel state (lock held inside _start_kernel/_warm_restart for _KERNEL_ACTIVITY)
            with _KERNEL_LOCK:
                _KERNEL_CLIENTS[kernel_id] = kc
                _DOCKER_CONTAINERS[kernel_id] = container_id
                if kernel_id in _STALE_CONTAINERS:
                    del _STALE_CONTAINERS[kernel_id]

        if needs_init:
            # First time — run initialization code (defines _M6CountdownTimer etc.)
            try:
                with open(INIT_CODE_FILE) as fin:
                    start_code = fin.read()
                    container_font_path = f'{self.container_work_dir}/{os.path.basename(ALIB_FONT_FILE)}'
                    start_code = start_code.replace('{{M6_FONT_PATH}}', repr(container_font_path)[1:-1])
                    start_code += '\n%xmode Minimal'
                self._execute_code(kc, start_code, timeout=exec_timeout, kernel_id=kernel_id)
            except Exception:
                # Init failed — clean up the broken kernel so next call recreates fresh
                with _KERNEL_LOCK:
                    if kernel_id in _KERNEL_CLIENTS:
                        try:
                            _KERNEL_CLIENTS[kernel_id].shutdown()
                        except Exception:
                            pass
                        del _KERNEL_CLIENTS[kernel_id]
                    if kernel_id in _DOCKER_CONTAINERS:
                        container_id = _DOCKER_CONTAINERS[kernel_id]
                        try:
                            subprocess.run(
                                ['docker', 'stop', container_id],
                                timeout=10, capture_output=True, encoding='utf-8', errors='replace'
                            )
                            subprocess.run(
                                ['docker', 'rm', container_id],
                                timeout=10, capture_output=True, encoding='utf-8', errors='replace'
                            )
                        except Exception as cleanup_err:
                            logger.warning(f"Container stop/rm failed during init error cleanup: {cleanup_err}")
                        del _DOCKER_CONTAINERS[kernel_id]
                raise  # Re-raise so caller sees the failure

        # Auto-resolve Windows paths to Docker container paths if enabled
        resolved_code = code
        path_resolve_count = 0
        if fix_paths:
            try:
                resolved_code, path_resolve_count = resolve_code_paths(code)
            except Exception as e:
                logger.warning(f"Path resolution failed: {e}. Executing code as-is.")
                resolved_code = code
                path_resolve_count = 0

        if exec_timeout:
            resolved_code = f'_M6CountdownTimer.start({exec_timeout})\n{resolved_code}'

        fixed_code = []
        for line in resolved_code.split('\n'):
            fixed_code.append(line)
            if line.startswith('sns.set_theme('):
                fixed_code.append('plt.rcParams["font.family"] = _m6_font_prop.get_name()')
        fixed_code = '\n'.join(fixed_code)
        fixed_code += '\n\n'  # Prevent code not executing in notebook due to no line breaks at the end
        
        try:
            result = self._execute_code(kc, fixed_code, timeout=exec_timeout, kernel_id=kernel_id)
        except TimeoutError as e:
            # On timeout, escalate through 3 tiers to recover the kernel
            logger.warning(f"Code interpreter execution timed out ({exec_timeout}s), escalating...")
            
            interrupted = False
            
            # Collect any partial output accumulated before the timeout
            # The TimeoutError carries partial_output in its args if available
            partial_result = ''
            if e.args and isinstance(e.args[0], dict):
                partial_result = e.args[0].get('partial_output', '')

            # Drain constants: prevent resource exhaustion from runaway IOPub message streams
            DRAIN_TOTAL_TIMEOUT = 5.0       # Max seconds spent draining IOPub messages per call
            DRAIN_MAX_MESSAGES = 100        # Max IOPub messages to read per drain cycle
            DRAIN_MAX_OUTPUT_BYTES = 10 * 1024 * 1024  # 10 MB cap on accumulated output

            # Tier 1: Jupyter-level interrupt + poll for idle status, collecting all remaining output
            try:
                kc.interrupt()
                for _ in range(3):
                    time.sleep(0.5)

                    with _KERNEL_LOCK:
                        if kernel_id in _WATCHDOG_KILLED:
                            break

                    # Drain ALL messages that arrived during the sleep window, not just one
                    interrupted_flag, output = self._drain_iopub(
                        kc, kernel_id, msg_timeout=1.0,
                        drain_total_timeout=DRAIN_TOTAL_TIMEOUT,
                        drain_max_messages=DRAIN_MAX_MESSAGES,
                        drain_max_output_bytes=DRAIN_MAX_OUTPUT_BYTES,
                    )
                    if interrupted_flag:
                        interrupted = True
                    partial_result += output
            except Exception as interrupt_err:
                logger.warning(f"Tier 1 interrupt failed: {interrupt_err}")

            # Tier 2: Docker-level SIGINT if still not interrupted
            if not interrupted:
                with _KERNEL_LOCK:
                    container_id = _DOCKER_CONTAINERS.get(kernel_id)

                if container_id:
                    try:
                        subprocess.run(
                            ['docker', 'exec', container_id, 'kill', '-INT', '1'],
                            timeout=5, capture_output=True, encoding='utf-8', errors='replace'
                        )
                        time.sleep(2)

                        # Drain ALL remaining messages after SIGINT, not just one
                        interrupted_flag, output = self._drain_iopub(
                            kc, kernel_id, msg_timeout=2.0,
                            drain_total_timeout=DRAIN_TOTAL_TIMEOUT,
                            drain_max_messages=DRAIN_MAX_MESSAGES,
                            drain_max_output_bytes=DRAIN_MAX_OUTPUT_BYTES,
                        )
                        if interrupted_flag:
                            interrupted = True
                        partial_result += output
                    except Exception as sigint_err:
                        logger.warning(f"Tier 2 docker SIGINT failed: {sigint_err}")
            
            # Tier 3: Kill the container entirely if still unresponsive.
            # Instead of fully removing it, keep a stale record for warm restart (reduces ~7s overhead).
            if not interrupted:
                with _KERNEL_LOCK:
                    container_id = _DOCKER_CONTAINERS.get(kernel_id)

                if container_id:
                    try:
                        subprocess.run(
                            ['docker', 'kill', '-s', 'KILL', container_id],
                            timeout=10, capture_output=True, encoding='utf-8', errors='replace'
                        )
                        # Don't remove the container — keep it for warm restart on next call.
                        # Record as stale with timestamp for TTL-based cleanup (prevents accumulation).
                        with _KERNEL_LOCK:
                            _STALE_CONTAINERS[kernel_id] = {'container_id': container_id, 'timestamp': time.time()}
                            if kernel_id in _DOCKER_CONTAINERS:
                                del _DOCKER_CONTAINERS[kernel_id]

                        logger.info(f"Tier 3: Container {container_id} killed (kept for warm restart)")
                    except Exception as kill_err:
                        logger.warning(f"Tier 3 container kill failed: {kill_err}")

                # Also clean up the dead kernel client so next call starts fresh
                with _KERNEL_LOCK:
                    if kernel_id in _KERNEL_CLIENTS:
                        try:
                            _KERNEL_CLIENTS[kernel_id].shutdown()
                        except Exception:
                            pass
                        del _KERNEL_CLIENTS[kernel_id]

            # Update activity timestamp so watchdog doesn't double-kill
            with _KERNEL_LOCK:
                if kernel_id in _KERNEL_ACTIVITY and isinstance(_KERNEL_ACTIVITY[kernel_id], dict):
                    _KERNEL_ACTIVITY[kernel_id]['last_active'] = time.time()
                else:
                    _KERNEL_ACTIVITY[kernel_id] = {'last_active': time.time(), 'work_dir': self.work_dir}
            
            # Return timeout message along with any partial output collected
            if exec_timeout and isinstance(e, TimeoutError):
                timeout_msg = f'Timeout: Code execution exceeded the {exec_timeout}-second time limit.'
                if partial_result.strip():
                    return f'{timeout_msg}\n\nPartial output:\n{partial_result}'
                return f'{timeout_msg}. Please optimize your code or break it into smaller steps.'
            raise
        except Exception as e:
            # Check if the kernel was killed by the watchdog while we were executing (thread-safe)
            with _KERNEL_LOCK:
                was_killed = kernel_id in _WATCHDOG_KILLED
            if was_killed:
                logger.warning(
                    f"Code interpreter execution failed because the kernel was killed "
                    f"by the watchdog (inactive for {CONTAINER_WATCHDOG_TIMEOUT}s). "
                    f"Original error: {e}"
                )
                return (
                    f'ERROR: Code interpreter kernel was terminated due to inactivity '
                    f'(no response for {CONTAINER_WATCHDOG_TIMEOUT} seconds). '
                    f'The next code_interpreter call will start a fresh kernel. '
                    f'Please try again.'
                )
            # Re-raise any other exceptions
            raise

        if exec_timeout:
            try:
                self._execute_code(kc, '_M6CountdownTimer.cancel()', timeout=10, kernel_id=kernel_id)
            except Exception as e:
                logger.debug(f"Cancel timer failed (non-critical): {e}")

        # Update activity timestamp so watchdog knows this kernel is still healthy
        with _KERNEL_LOCK:
            if kernel_id in _KERNEL_ACTIVITY and isinstance(_KERNEL_ACTIVITY[kernel_id], dict):
                _KERNEL_ACTIVITY[kernel_id]['last_active'] = time.time()

        # Add path resolution feedback if paths were auto-resolved
        path_notice = build_path_resolution_notice(path_resolve_count)
        
        if not result.strip():
            if path_notice:
                return f'Finished execution.\n\n{path_notice}'
            return 'Finished execution.'

        # Get the truncation limit from agent/tool options
        char_limit = 2000
        agent_obj = kwargs.get('agent_obj')
        agent_pool = getattr(agent_obj, 'agent_pool', None)
        
        if agent_pool:
            llm_cfg = getattr(agent_pool, 'llm_cfg', {})
            char_limit = llm_cfg.get('code_char_limit', char_limit)
        elif self.cfg.get('code_char_limit'):
            char_limit = self.cfg.get('code_char_limit')

        # Append path resolution notice to result (before truncation check so it's included in spill file)
        if path_notice:
            result = f'{result}\n\n{path_notice}'

        if char_limit != -1 and len(result) > char_limit:
            # Save full result to spill file (use work_dir from config for correct path resolution)
            log_dir = Path(self.work_dir) / 'logs' / 'spillover'
            log_dir.mkdir(parents=True, exist_ok=True)
            
            instance_name = kwargs.get('agent_instance_name', 'unknown')
            
            # Cap output to prevent disk exhaustion from massive code interpreter outputs
            if len(result) > MAX_SPILL_SIZE:
                result_copy = result[:MAX_SPILL_SIZE] + "\n\n[SPILL FILE TRUNCATED — exceeded maximum size]"
            else:
                result_copy = result
            
            # Use shared generate_spillover_filename helper for collision detection with counter cap < 1000
            spill_filename = generate_spillover_filename(instance_name, 'code_interpreter', log_dir)
            spill_path = log_dir / spill_filename
            
            try:
                spill_path.write_text(result_copy, encoding='utf-8')
                rel_spill = str(spill_path)
                if agent_pool and agent_pool.operation_manager:
                    try:
                        rel_spill = str(spill_path.relative_to(agent_pool.operation_manager.base_dir))
                    except ValueError:
                        pass
            except Exception as e:
                rel_spill = f"ERROR SAVING SPILL: {e}"

            result = result[:char_limit] + f"\n\n[TOOL RESPONSE TRUNCATED — Character limit exceeded. Full output saved to: {rel_spill}]"

        return result

    @staticmethod
    def _drain_iopub(
        kc, kernel_id: str, msg_timeout: float = 1.0,
        drain_total_timeout: float = 5.0,
        drain_max_messages: int = 100,
        drain_max_output_bytes: int = 10 * 1024 * 1024,
    ) -> tuple[bool, str]:
        """Read all available IOPub messages until queue is empty or timeout.

        Guards against resource exhaustion:
        - Total drain time capped at drain_total_timeout seconds
        - Message count capped at drain_max_messages per call
        - Accumulated output size capped at drain_max_output_bytes (10 MB)

        Args:
            kc: The kernel client.
            kernel_id: Kernel identifier for watchdog checks and logging.
            msg_timeout: Timeout for each individual message read.
            drain_total_timeout: Max total seconds spent draining.
            drain_max_messages: Max IOPub messages to read per drain cycle.
            drain_max_output_bytes: Cap on accumulated output size.

        Returns:
            Tuple of (interrupted, partial_output) where interrupted is True if
            an idle status was received, and partial_output contains collected text.
        """
        interrupted = False
        parts: list[str] = []
        start = time.time()
        msg_count = 0

        while True:
            if time.time() - start >= drain_total_timeout:
                break
            if msg_count >= drain_max_messages:
                break
            # Check watchdog before the blocking get_iopub_msg call (reduces latency)
            with _KERNEL_LOCK:
                if kernel_id in _WATCHDOG_KILLED:
                    break

            try:
                msg = kc.get_iopub_msg(timeout=msg_timeout)
            except queue.Empty:
                break
            except Exception as err:
                logger.debug(f"IOPub drain error for kernel {kernel_id}: {err}")
                break

            msg_count += 1
            mtype = msg['msg_type']
            content = msg['content']

            if mtype == 'status':
                if content.get('execution_state') == 'idle':
                    interrupted = True
            elif mtype in ('execute_result', 'display_data'):
                text = content['data'].get('text/plain', '')
                if text:
                    parts.append(f'\n\nstdout:\n```\n{text}\n```')
            elif mtype == 'stream':
                name = content['name']
                text = content['text']
                if text:
                    parts.append(f'\n\n{name}:\n```\n{text}\n```')
            elif mtype == 'error':
                text = _escape_ansi('\n'.join(content['traceback']))
                if text:
                    parts.append(f'\n\nstderr:\n```\n{text}\n```')
            else:
                logger.debug(
                    f"IOPub drain: ignoring unknown msg type '{mtype}' "
                    f"for kernel {kernel_id}"
                )

        partial_output = ''.join(parts) if parts else ''
        if len(partial_output) > drain_max_output_bytes:
            kept = partial_output[:drain_max_output_bytes - 50]
            partial_output = (
                f'{kept}\n\n[OUTPUT TRUNCATED — exceeded '
                f'{drain_max_output_bytes // (1024*1024)}MB limit]'
            )

        return interrupted, partial_output

    def __del__(self):
        # Recycle the jupyter subprocess and Docker container:
        k: str = f'{self.instance_id}_{os.getpid()}'
        with _KERNEL_LOCK:
            if k in _KERNEL_CLIENTS:
                try:
                    _KERNEL_CLIENTS[k].shutdown()
                except Exception:
                    pass
                del _KERNEL_CLIENTS[k]
            if k in _DOCKER_CONTAINERS:
                container_id = _DOCKER_CONTAINERS[k]
                # Force-remove container (handles both running and stopped states)
                try:
                    subprocess.run(['docker', 'rm', '-f', container_id], timeout=10, capture_output=True, encoding='utf-8', errors='replace')
                except Exception:
                    pass
                finally:
                    del _DOCKER_CONTAINERS[k]

            # Also clean up stale container record if it exists
            if k in _STALE_CONTAINERS:
                stale_cid = _STALE_CONTAINERS[k]['container_id']
                try:
                    subprocess.run(['docker', 'rm', '-f', stale_cid], timeout=10, capture_output=True, encoding='utf-8', errors='replace')
                except Exception:
                    pass
                finally:
                    del _STALE_CONTAINERS[k]

        # Clean up path mapping file for this kernel
        mapping_file = os.path.join(self.work_dir, f'path_mapping_{k}.json')
        try:
            if os.path.exists(mapping_file):
                os.remove(mapping_file)
        except OSError:
            pass

    def _is_path_allowed(self, abs_path: str, allowed_prefixes: List[str]) -> bool:
        """Check if a path is within an allowed directory using proper containment check.
        
        Uses os.path.commonpath() instead of .startswith() to prevent sibling-directory escape.
        E.g., /workspace_extra would pass .startswith('/workspace') but fails commonpath check.
        """
        for prefix in allowed_prefixes:
            try:
                if os.path.commonpath([abs_path, prefix]) == prefix:
                    return True
            except ValueError:
                # Different drive letters on Windows (e.g., C:\ vs D:\)
                continue
        return False

    def _resolve_extra_folders(self):
        """Resolve extra work folders, reading from operation_manager if available for dynamic config.
        
        Falls back to stored defaults if no operation_manager is set (e.g., standalone use).
        Returns:
            Tuple of (extra_rw_list, extra_ro_list) as lists of strings.
        """
        if self._operation_manager is not None:
            om = self._operation_manager
            extra_rw = [str(p) for p in getattr(om, 'extra_work_folders_rw', [])]
            extra_ro = [str(p) for p in getattr(om, 'extra_work_folders_ro', [])]
        else:
            # Fall back to config-set values (backward compatible)
            # Note: In production via agent_factory.py, _operation_manager is always set,
            # so this path primarily serves standalone/testing use cases.
            extra_rw = list(self.extra_work_folders_rw)
            extra_ro = list(self.extra_work_folders_ro)
        return extra_rw, extra_ro

    def _build_path_mapping(self, kernel_id: str, mounted_rw: List[dict], mounted_ro: List[dict]) -> dict:
        """Build the path mapping dict for a kernel.
        
        Args:
            kernel_id: The kernel identifier.
            mounted_rw: List of {'host': ..., 'container': ...} dicts for RW mounts.
            mounted_ro: List of {'host': ..., 'container': ...} dicts for RO mounts.
        Returns:
            Path mapping dict ready to be serialized to JSON.
        """
        path_mapping = {
            'work_dir': self.container_work_dir,
            'extra_rw': [m['container'] for m in mounted_rw],
            'extra_ro': [m['container'] for m in mounted_ro],
        }
        path_mapping['host_to_container'] = {}
        path_mapping['host_to_container']['work_dir'] = {
            'host': os.path.abspath(self.work_dir),
            'container': self.container_work_dir,
        }
        for i, m in enumerate(mounted_rw):
            key = f'extra_rw_{i}'
            path_mapping['host_to_container'][key] = {
                'host': m['host'],
                'container': m['container'],
                'access': 'read-write',
            }
        for i, m in enumerate(mounted_ro):
            key = f'extra_ro_{i}'
            path_mapping['host_to_container'][key] = {
                'host': m['host'],
                'container': m['container'],
                'access': 'read-only',
            }
        return path_mapping

    def _build_docker_image(self):
        """Build Docker image from Dockerfile if not exists"""
        # Check if image already exists
        result = subprocess.run(
            ['docker', 'images', '-q', self.docker_image_name],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
        
        if result.stdout.strip():
            logger.info(f'Docker image {self.docker_image_name} already exists')
            return
                
        logger.info(f'Building Docker image {self.docker_image_name} from {DOCKER_IMAGE_FILE}')
        dockerfile_dir = os.path.dirname(os.path.abspath(DOCKER_IMAGE_FILE))
        
        build_process = subprocess.run(
            ['docker', 'build', '-t', self.docker_image_name, '-f', DOCKER_IMAGE_FILE, dockerfile_dir],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
        
        if build_process.returncode != 0:
            raise RuntimeError(f'Failed to build Docker image: {build_process.stderr}')
        
        logger.info(f'Successfully built Docker image {self.docker_image_name}')

    def _get_free_ports(self, n=5):
        ports = []
        sockets = []
        for _ in range(n):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(('', 0))
            ports.append(s.getsockname()[1])
            sockets.append(s)
        for s in sockets:
            s.close()
        return ports

    @staticmethod
    def _create_kernel_client(host_connection_file: str, container_id: str) -> 'BlockingKernelClient':
        """Create a Jupyter kernel client, start channels, and wait for readiness.

        Shared helper used by both _start_kernel and _warm_restart_kernel to avoid
        duplicating connection setup and readiness-wait logic (~60% overlap).

        Args:
            host_connection_file: Path to the host-side connection JSON file.
            container_id: Container ID (used for log retrieval on failure).

        Returns:
            A ready BlockingKernelClient instance.
        """
        from jupyter_client import BlockingKernelClient

        kc = BlockingKernelClient(connection_file=host_connection_file)
        asyncio.set_event_loop_policy(AnyThreadEventLoopPolicy())
        kc.load_connection_file()
        kc.start_channels()

        max_retries = 3
        for attempt in range(max_retries):
            try:
                kc.wait_for_ready(timeout=10)
                logger.info(f"Kernel is ready (attempt {attempt+1}/{max_retries})")
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(f"Kernel not ready (attempt {attempt + 1}/{max_retries}), retrying...")
                    time.sleep(2)
                else:
                    logs = subprocess.run(
                        ['docker', 'logs', container_id],
                        capture_output=True, text=True, encoding='utf-8', errors='replace'
                    )
                    raise RuntimeError(
                        f'Kernel failed to start: {e}\nContainer logs:\n{logs.stdout}\n{logs.stderr}'
                    )

        return kc

    def _start_kernel(self, kernel_id: str):
        self._build_docker_image()

        host_connection_file = os.path.join(self.work_dir, f'kernel_connection_file_{kernel_id}_host.json')
        container_connection_file = os.path.join(self.work_dir, f'kernel_connection_file_{kernel_id}_container.json')
        launch_kernel_script = os.path.join(self.work_dir, f'launch_kernel_{kernel_id}.py')

        for f in [host_connection_file, container_connection_file, launch_kernel_script]:
            if os.path.exists(f):
                logger.info(f'WARNING: {f} already exists')
                os.remove(f)

        os.makedirs(self.work_dir, exist_ok=True)
        with open(launch_kernel_script, 'w') as fout:
            fout.write(LAUNCH_KERNEL_PY)

        work_dir_font = os.path.join(self.work_dir, os.path.basename(ALIB_FONT_FILE))
        if not os.path.exists(work_dir_font):
            shutil.copy(ALIB_FONT_FILE, work_dir_font)

        # prepare host connection file
        host_conn_data = {
            "ip": "127.0.0.1",
            "key": str(uuid.uuid4()),
            "transport": "tcp",
            "signature_scheme": "hmac-sha256",
            "kernel_name": ""
        }
        ports = self._get_free_ports(5)
        port_names = ['shell_port', 'iopub_port', 'stdin_port', 'hb_port', 'control_port']
        port_config = dict(zip(port_names, ports))
        host_conn_data.update(port_config)
        with open(host_connection_file, 'w') as f:
            json.dump(host_conn_data, f)

        # Prepare container connection file: use 0.0.0.0 inside container for Windows Docker port forwarding
        # Kernel binds to all interfaces, while host-side ports are restricted to 127.0.0.1
        container_conn_data = host_conn_data.copy()
        container_conn_data["ip"] = "0.0.0.0"
        with open(container_connection_file, 'w') as f:
            json.dump(container_conn_data, f)

        # Resolve extra folders dynamically (picks up runtime config changes if operation_manager is set)
        extra_rw, extra_ro = self._resolve_extra_folders()

        # Track which extra folders were actually mounted (for path mapping)
        mounted_rw = []
        mounted_ro = []

        # Allowed prefixes for path security validation — prevent mounting arbitrary host paths
        # Use work_dir as the allowed root; also add extra folder paths themselves since they
        # come from trusted config and may be siblings of work_dir (not children)
        allowed_prefixes = {os.path.realpath(self.work_dir)} if self.work_dir else set()
        for fp in [*extra_rw, *extra_ro]:
            rp = os.path.realpath(fp)
            allowed_prefixes.add(rp)

        # Mount extra RW work folders as /extra_rw_0, /extra_rw_1, etc.
        for folder_path in extra_rw:
            abs_path = os.path.realpath(folder_path)  # resolves symlinks for security check
            if not os.path.isdir(abs_path):
                logger.warning("Extra RW mount path does not exist, skipping: %s", abs_path)
                continue
            if not self._is_path_allowed(abs_path, allowed_prefixes):
                logger.warning("Extra RW mount path %s is outside allowed directories, skipping", abs_path)
                continue
            mount_point = f'/extra_rw_{len(mounted_rw)}'
            mounted_rw.append({'host': abs_path, 'container': mount_point})

        # Mount extra RO work folders as /extra_ro_0, /extra_ro_1, etc. (read-only)
        for folder_path in extra_ro:
            abs_path = os.path.realpath(folder_path)  # resolves symlinks for security check
            if not os.path.isdir(abs_path):
                logger.warning("Extra RO mount path does not exist, skipping: %s", abs_path)
                continue
            if not self._is_path_allowed(abs_path, allowed_prefixes):
                logger.warning("Extra RO mount path %s is outside allowed directories, skipping", abs_path)
                continue
            mount_point = f'/extra_ro_{len(mounted_ro)}'
            mounted_ro.append({'host': abs_path, 'container': mount_point})

        # Create path mapping data (file is written AFTER container starts to avoid orphaned files)
        path_mapping = self._build_path_mapping(kernel_id, mounted_rw, mounted_ro)
        path_mapping_file = os.path.join(self.work_dir, f'path_mapping_{kernel_id}.json')

        # Remove any leftover container with the same name from a previous crash
        container_name = f'code_interpreter_{kernel_id}'
        subprocess.run(
            ['docker', 'rm', '-f', container_name],
            capture_output=True, text=True, timeout=10, encoding='utf-8', errors='replace'
        )

        # prepare Docker launch cmd
        docker_run_cmd = [
            'docker', 'run', '-d',
            '--name', container_name,
            # Drop all Linux capabilities and prevent privilege escalation
            '--cap-drop=ALL',
            '--security-opt=no-new-privileges',
            # Enforce resource limits (memory, CPU, PID count) to prevent container exhaustion
            '--memory=' + CONTAINER_MEMORY_LIMIT,
            '--cpus=' + str(CONTAINER_CPU_LIMIT),
            '--pids-limit=' + str(CONTAINER_PID_LIMIT),
        ]

        # Mount extra RW work folders as /extra_rw_0, /extra_rw_1, etc.
        # NOTE: These mounts are added BEFORE the main work_dir mount so Docker processes
        # the more specific paths first, avoiding overlay filesystem stacking issues where
        # writes to subdirectories don't persist back to the host disk (Fix MountStacking).
        for folder_path in extra_rw:
            abs_path = os.path.realpath(folder_path)
            if not os.path.isdir(abs_path):
                continue
            mount_point = f'/extra_rw_{len(mounted_rw)}'
            docker_run_cmd.extend(['-v', f'{abs_path}:{mount_point}'])

        # Mount extra RO work folders as /extra_ro_0, /extra_ro_1, etc. (read-only)
        for folder_path in extra_ro:
            abs_path = os.path.realpath(folder_path)
            if not os.path.isdir(abs_path):
                continue
            mount_point = f'/extra_ro_{len(mounted_ro)}'
            docker_run_cmd.extend(['-v', f'{abs_path}:{mount_point}:ro'])

        # Mount main work directory AFTER extra mounts so specific subdirectory mounts take precedence.
        docker_run_cmd.extend([
            '-v', f'{os.path.abspath(self.work_dir)}:{self.container_work_dir}',
            '-w', self.container_work_dir,
        ])

        # Bind forwarded ports to 127.0.0.1 only (not all interfaces, for security)
        for p in ports:
            docker_run_cmd.extend(['-p', f'127.0.0.1:{p}:{p}'])

        docker_run_cmd.extend([
            self.docker_image_name,
            'python', f'{self.container_work_dir}/{os.path.basename(launch_kernel_script)}',
            '--IPKernelApp.connection_file',
            f'{self.container_work_dir}/{os.path.basename(container_connection_file)}',
            '--KernelApp.allow_remote_access=False',
            '--matplotlib=inline',
            '--quiet',
        ])

        # start Docker container
        result = subprocess.run(docker_run_cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.returncode != 0:
            raise RuntimeError(f'Failed to start Docker container: {result.stderr}')

        # Container started successfully — now write the path mapping file (avoids orphaned files on failure)
        with open(path_mapping_file, 'w') as f:
            json.dump(path_mapping, f, indent=2)

        # Update code_path_resolver with actual mounts so path resolution matches reality
        set_active_mappings(path_mapping['host_to_container'])

        container_id = result.stdout.strip()
        logger.info(f"INFO: Docker container ID = {container_id}")

        # Create client and wait for kernel readiness (shared helper)
        kc = self._create_kernel_client(host_connection_file, container_id)

        # Initialize activity tracking for watchdog (include work_dir for cleanup)
        with _KERNEL_LOCK:
            _KERNEL_ACTIVITY[kernel_id] = {'last_active': time.time(), 'work_dir': self.work_dir}

        return kc, container_id

    def _warm_restart_kernel(self, kernel_id: str, container_id: str):
        """Reconnect to an existing Docker container and launch a new kernel inside it.

        The container_id is passed from the caller (call()) which already located it,
        avoiding redundant docker ps calls. This saves ~7s by skipping docker rm + run.

        Args:
            kernel_id: Kernel identifier.
            container_id: Container ID discovered by the caller.

        Returns:
            Tuple of (kernel_client, container_id)
        """
        host_connection_file = os.path.join(self.work_dir, f'kernel_connection_file_{kernel_id}_host.json')
        container_connection_file = os.path.join(self.work_dir, f'kernel_connection_file_{kernel_id}_container.json')

        # Read original ports from existing connection file BEFORE deleting it.
        # The container has fixed port bindings that we must reuse for traffic forwarding.
        original_ports = None
        if os.path.exists(host_connection_file):
            try:
                with open(host_connection_file, 'r') as f:
                    old_conn_data = json.load(f)
                port_names = ['shell_port', 'iopub_port', 'stdin_port', 'hb_port', 'control_port']
                original_ports = [old_conn_data.get(p) for p in port_names]
                if None in original_ports:
                    logger.warning(f"Warm restart: incomplete port data in connection file, falling back to full start")
                    raise RuntimeError("Invalid connection file, cannot reuse ports")
            except Exception as e:
                logger.warning(f"Warm restart: failed to read old connection file ({e}), falling back to full start")
                raise RuntimeError("Failed to read connection file, cannot reuse ports")

        # Delete old connection files so we start fresh
        for f in [host_connection_file, container_connection_file]:
            if os.path.exists(f):
                os.remove(f)

        # Prepare host connection file with original ports (or new ones if not available)
        host_conn_data = {
            "ip": "127.0.0.1",
            "key": str(uuid.uuid4()),
            "transport": "tcp",
            "signature_scheme": "hmac-sha256",
            "kernel_name": ""
        }
        port_names = ['shell_port', 'iopub_port', 'stdin_port', 'hb_port', 'control_port']
        if original_ports:
            ports = original_ports
            logger.info(f"Warm restart: reusing original ports {ports}")
        else:
            ports = self._get_free_ports(5)
            logger.info(f"Warm restart: no original ports found, using new ports {ports}")

        port_config = dict(zip(port_names, ports))
        host_conn_data.update(port_config)
        with open(host_connection_file, 'w') as f:
            json.dump(host_conn_data, f)

        # Prepare container connection file (use 0.0.0.0 inside container for Windows Docker port forwarding)
        container_conn_data = host_conn_data.copy()
        container_conn_data["ip"] = "0.0.0.0"
        with open(container_connection_file, 'w') as f:
            json.dump(container_conn_data, f)

        # Launch a new kernel process inside the existing container using docker exec.
        launch_script = os.path.join(self.work_dir, f'launch_kernel_{kernel_id}.py')
        if not os.path.exists(launch_script):
            os.makedirs(self.work_dir, exist_ok=True)
            with open(launch_script, 'w') as fout:
                fout.write(LAUNCH_KERNEL_PY)

        exec_cmd = [
            'docker', 'exec', '-i', container_id,
            'python', f'{self.container_work_dir}/{os.path.basename(launch_script)}',
            '--IPKernelApp.connection_file',
            f'{self.container_work_dir}/{os.path.basename(container_connection_file)}',
            '--KernelApp.allow_remote_access=False',
            '--matplotlib=inline',
            '--quiet',
        ]

        # Start kernel process in background inside container (single docker call)
        subprocess.Popen(exec_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        time.sleep(2)

        # Create client and wait for kernel readiness
        kc = self._create_kernel_client(host_connection_file, container_id)

        # Initialize activity tracking for watchdog
        with _KERNEL_LOCK:
            _KERNEL_ACTIVITY[kernel_id] = {'last_active': time.time(), 'work_dir': self.work_dir}

        return kc, container_id

    def _execute_code(self, kc, code: str, timeout: Optional[int] = None, kernel_id: Optional[str] = None) -> str:
        """Execute code in the Jupyter kernel with a message-level timeout.
        
        Args:
            kc: The kernel client connection.
            code: Python code to execute.
            timeout: Maximum seconds to wait for each IOPub message (default: CODE_EXECUTION_TIMEOUT).
                    Set to None to disable timeout (not recommended).
            kernel_id: Kernel identifier for watchdog tracking (passed from caller).
                      Defaults to self.instance_id_pid if not provided.
        
        Returns:
            Formatted string with stdout, stderr, execution results, and images.
        
        Raises:
            TimeoutError: If code execution exceeds the time limit.
        """
        if kernel_id is None:
            logger.warning("kernel_id not passed to _execute_code; using default")
            kernel_id = f'{self.instance_id}_{os.getpid()}'
        if timeout is None:
            timeout = CODE_EXECUTION_TIMEOUT

        # Wait for kernel readiness with timeout to prevent permanent hang on stuck kernels
        try:
            kc.wait_for_ready(timeout=30)
        except Exception as e:
            logger.warning(f"Kernel wait_for_ready failed: {e}")
            raise
        
        # Drain any leftover messages from the probe execute before sending real code
        try:
            while True:
                m = kc.get_iopub_msg(timeout=0.5)
        except:
            pass
        
        kc.execute(code)

        # Mark execution start as "active" so watchdog doesn't kill during CPU-bound computation
        with _KERNEL_LOCK:
            if kernel_id in _KERNEL_ACTIVITY and isinstance(_KERNEL_ACTIVITY[kernel_id], dict):
                _KERNEL_ACTIVITY[kernel_id]['last_active'] = time.time()
        
        result = ''
        image_idx = 0
        
        # OVERALL wall-clock budget: prevents runaway execution when the kernel
        # keeps producing output (e.g. rglob scanning thousands of files). Each
        # individual message still has a per-message timeout to catch kernel hangs.
        start_time = time.time()
        per_message_timeout = min(10, timeout)  # use 10s per-message, or the overall budget if smaller

        while True:
            # Check if the kernel was killed by the watchdog during execution (thread-safe)
            with _KERNEL_LOCK:
                was_killed = kernel_id in _WATCHDOG_KILLED
            if was_killed:
                result = ''  # Discard partial output — it's stale/unreliable
                text = (
                    f'ERROR: The code interpreter kernel was terminated due to '
                    f'inactivity (no response for {CONTAINER_WATCHDOG_TIMEOUT} seconds). '
                    f'The next call will start a fresh kernel.'
                )
                finished = True
                break
            
            # Check overall wall-clock budget at the top of each iteration
            # Raise TimeoutError so the caller's except block can interrupt the kernel
            if time.time() - start_time > timeout:
                raise TimeoutError({'partial_output': result, 'message': f'Code execution exceeded the {timeout}-second time limit.'})
            
            text = ''
            image = ''
            finished = False
            msg_type = 'error'
            try:
                # Per-message timeout catches kernel hangs (no output at all)
                msg = kc.get_iopub_msg(timeout=per_message_timeout)
                
                # C2 fix: check for watchdog kill immediately after get_iopub_msg returns,
                # before processing the message — a kill could happen during the blocking call
                with _KERNEL_LOCK:
                    was_killed = kernel_id in _WATCHDOG_KILLED
                if was_killed:
                    result = ''  # Discard partial output
                    text = (
                        f'ERROR: The code interpreter kernel was terminated due to '
                        f'inactivity (no response for {CONTAINER_WATCHDOG_TIMEOUT} seconds). '
                        f'The next call will start a fresh kernel.'
                    )
                    finished = True
                    break
                
                msg_type = msg['msg_type']
                if msg_type == 'status':
                    if msg['content'].get('execution_state') == 'idle':
                        finished = True
                elif msg_type == 'execute_result':
                    text = msg['content']['data'].get('text/plain', '')
                    if 'image/png' in msg['content']['data']:
                        image_b64 = msg['content']['data']['image/png']
                        image_url = self._serve_image(image_b64)
                        image_idx += 1
                        image = '![fig-%03d](%s)' % (image_idx, image_url)
                elif msg_type == 'display_data':
                    if 'image/png' in msg['content']['data']:
                        image_b64 = msg['content']['data']['image/png']
                        image_url = self._serve_image(image_b64)
                        image_idx += 1
                        image = '![fig-%03d](%s)' % (image_idx, image_url)
                    else:
                        text = msg['content']['data'].get('text/plain', '')
                elif msg_type == 'stream':
                    msg_type = msg['content']['name']  # stdout, stderr
                    text = msg['content']['text']
                elif msg_type == 'error':
                    text = _escape_ansi('\n'.join(msg['content']['traceback']))
                    if 'M6_CODE_INTERPRETER_TIMEOUT' in text:
                        text = f'Timeout: Code execution exceeded the {timeout}-second time limit.'
            except queue.Empty:
                # Raised by get_iopub_msg() when the per-message timeout expires
                raise TimeoutError({'partial_output': result, 'message': f'Code execution exceeded the {timeout}-second time limit.'})
            except Exception as e:
                logger.debug(f"Unexpected IOPub error during execution for kernel {kernel_id}: {e}")
                text = 'The code interpreter encountered an unexpected error.'
                print_traceback()
                finished = True
            
            # Update kernel activity timestamp for watchdog (thread-safe)
            with _KERNEL_LOCK:
                # Preserve the dict structure (watchdog reads _KERNEL_ACTIVITY[kernel_id].get('work_dir'))
                if kernel_id in _KERNEL_ACTIVITY and isinstance(_KERNEL_ACTIVITY[kernel_id], dict):
                    _KERNEL_ACTIVITY[kernel_id]['last_active'] = time.time()
                else:
                    _KERNEL_ACTIVITY[kernel_id] = {'last_active': time.time(), 'work_dir': self.work_dir}

            if text:
                result += f'\n\n{msg_type}:\n\n```\n{text}\n```'
            if image:
                result += f'\n\n{image}'
            if finished:
                break
        result = result.lstrip('\n')
        return result

    def _serve_image(self, image_base64: str) -> str:
        import PIL.Image

        image_file = f'{uuid.uuid4()}.png'
        local_image_file = os.path.join(self.work_dir, image_file)

        png_bytes = base64.b64decode(image_base64)
        assert isinstance(png_bytes, bytes)
        bytes_io = io.BytesIO(png_bytes)
        PIL.Image.open(bytes_io).save(local_image_file, 'png')

        image_server_url = os.getenv('M6_CODE_INTERPRETER_STATIC_URL', '')
        if image_server_url:
            return f'{image_server_url}/{image_file}'
        return local_image_file


def _check_docker_availability():
    try:
        result = subprocess.run(
            ['docker', '--version'],
            capture_output=True,
            text=True,
            timeout=5,
            encoding='utf-8',
            errors='replace'
        )
        if result.returncode != 0:
            raise RuntimeError('Docker is not available')
        
        result = subprocess.run(
            ['docker', 'info'],
            capture_output=True,
            text=True,
            timeout=5,
            encoding='utf-8',
            errors='replace'
        )
        if result.returncode != 0:
            raise RuntimeError('Docker daemon is not running')
        
        logger.info('Docker is available and running')
    except FileNotFoundError:
        raise RuntimeError('Docker is not installed. Please install Docker first.')
    except subprocess.TimeoutExpired:
        raise RuntimeError('Docker command timed out. Please check Docker installation.')
    except Exception as e:
        raise RuntimeError(f'Failed to check Docker availability: {str(e)}')


def _check_host_deps():
    """Check if host has required dependencies to connect to Docker container kernel"""
    try:
        from jupyter_client import BlockingKernelClient  # noqa
        import PIL.Image  # noqa
    except ImportError as e:
        raise ImportError(
            'The dependencies for Code Interpreter support are not installed. '
            'Please install the required dependencies by running: pip install "agent-cascade[code_interpreter]"') from e


def _escape_ansi(line: str) -> str:
    ansi_escape = re.compile(r'(?:\x1B[@-_]|[\x80-\x9F])[0-?]*[ -/]*[@-~]')
    return ansi_escape.sub('', line)


#
# The _BasePolicy and AnyThreadEventLoopPolicy below are borrowed from Tornado.
# Ref: https://www.tornadoweb.org/en/stable/_modules/tornado/platform/asyncio.html#AnyThreadEventLoopPolicy
#

if sys.platform == 'win32' and hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
    _BasePolicy = asyncio.WindowsSelectorEventLoopPolicy  # type: ignore
else:
    _BasePolicy = asyncio.DefaultEventLoopPolicy


class AnyThreadEventLoopPolicy(_BasePolicy):  # type: ignore
    """Event loop policy that allows loop creation on any thread.

    The default `asyncio` event loop policy only automatically creates
    event loops in the main threads. Other threads must create event
    loops explicitly or `asyncio.get_event_loop` (and therefore
    `.IOLoop.current`) will fail. Installing this policy allows event
    loops to be created automatically on any thread.

    Usage::
        asyncio.set_event_loop_policy(AnyThreadEventLoopPolicy())
    """

    def get_event_loop(self) -> asyncio.AbstractEventLoop:
        try:
            return super().get_event_loop()
        except RuntimeError:
            # "There is no current event loop in thread %r"
            loop = self.new_event_loop()
            self.set_event_loop(loop)
            return loop
