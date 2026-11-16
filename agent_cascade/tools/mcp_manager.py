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
import concurrent.futures
import datetime
import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Dict, List, Optional, Union

from dotenv import load_dotenv

from agent_cascade.log import logger
from agent_cascade.tools.base import BaseTool
from agent_cascade.utils.media_utils import save_media_to_media, MediaStorageError


# BUG_0310 R1 — upper bound on the MCP connect + handshake wait. initConfig used to
# call future.result() with NO timeout, so a slow/offline `npx` subprocess spawn could
# hang the boot thread for minutes. A generous 60 s default so a legitimately slow first
# `npx` resolve is not mistaken for a hang; override via AGENT_CASCADE_MCP_CONNECT_TIMEOUT_S.
# On expiry the wait raises concurrent.futures.TimeoutError, which flows into the existing
# handled failure path (config_handlers._handle_mcp_servers logs "[MCP] Eager initialization
# failed") — boot continues WITHOUT MCP, no new crash path. The drop is logged loudly so it
# is never a silent "MCP quietly disabled".
MCP_CONNECT_TIMEOUT_S: float = float(os.getenv('AGENT_CASCADE_MCP_CONNECT_TIMEOUT_S', 60.0))


# BUG_0310 follow-up — lazy / on-use connect. Teardown margin added to the dispatcher's
# DMS `_in_blocking_tool` bound around an on-use connect (see lazy_connect_dms_bound), so
# the holder is shielded slightly past the hard connect cap.
_MCP_CONNECT_TEARDOWN_MARGIN_S: float = 5.0


# Canonical location of the lazy tool-metadata cache (config/mcp_tools_cache.json).
# mcp_manager.py lives at <root>/agent_cascade/tools/, so repo root is grandparent.
MCP_TOOLS_CACHE_PATH: Path = Path(__file__).resolve().parent.parent.parent / 'config' / 'mcp_tools_cache.json'


def _lazy_settings():
    """Lazy accessor for agent_cascade.settings. Imported inside functions (NOT at module
    top) because mcp_manager is imported early in agent_cascade's own import chain and a
    top-level `from agent_cascade import settings` can hit a partially-initialized package."""
    from agent_cascade import settings as _s
    return _s


def lazy_connect_dms_bound(timeout_s: Optional[float] = None) -> float:
    """Return the `_in_blocking_tool` bound to wrap an ensure_connected() call so the holder
    is not DMS-terminated during a legitimately-slow bounded connect.

    connect_server applies its timeout to BOTH the connect handshake AND the tools/list
    step sequentially, so the calling thread can block up to 2×timeout_s wall-clock. The
    bound is derived from the SAME knob (settings.MCP_LAZY_CONNECT_TIMEOUT_S) plus a
    teardown margin, so it is always ≥ the real connect worst-case — never shorter, which
    would let the DMS kill a holder mid-connect-tail (the wedging/clobber the directive
    calls out). Mirrors the call_cli_agent _CLI_AGENT_TEARDOWN_MARGIN_S pattern.

    Sized for the direct-connect worst case; the rare waiter-reattempt tail (a holder that
    parks mid-connect and a waiter then re-attempts) can exceed this bound, and is
    intentionally not covered.
    """
    if timeout_s is None:
        timeout_s = _lazy_settings().MCP_LAZY_CONNECT_TIMEOUT_S
    return 2 * float(timeout_s) + _MCP_CONNECT_TEARDOWN_MARGIN_S


class MCPManager:
    _instance = None  # Private class variable to store the unique instance
    _registered_tool_names: set[str] = set()  # Track all dynamically registered MCP tool names

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(MCPManager, cls).__new__(cls, *args, **kwargs)
        return cls._instance

    @classmethod
    def is_mcp_tool(cls, tool_name: str) -> bool:
        """Check if a given tool name is a dynamically registered MCP tool."""
        return tool_name in cls._registered_tool_names

    def __init__(self):
        if not hasattr(self, 'clients'):  # The singleton should only be inited once
            """Set a new event loop in a separate thread"""
            try:
                import mcp  # noqa
            except ImportError as e:
                raise ImportError('Could not import mcp. Please install mcp with `pip install -U mcp`.') from e

            load_dotenv()  # Load environment variables from .env file
            self.clients: dict = {}
            self._connected_servers: dict[str, str] = {}  # server_name:config_hash → client_id
            self._server_tools: dict[str, list] = {}  # server_name → cached tools (for re-registration)
            self._mcp_lock = threading.Lock()  # Guards _connected_servers/_server_tools/_parked_servers/_connecting/_suppressed/_tool_server_map vs shutdown()
            # Lazy / on-use connect state (BUG_0310 follow-up). All guarded by _mcp_lock.
            # _parked_servers: server_name → conn_config (config known, NO live client).
            # _connecting: server_name → threading.Event; the in-flight single-flight guard
            #   (Q4: NOT _mcp_lock — that is a plain Lock with no wait/notify).
            # _suppressed: servers turned off (all tools disabled for every agent, OR
            #   config enabled:false) — ensure_connected refuses them.
            # _config_disabled: subset of _suppressed from enabled:false; a tool-toggle can
            #   never un-suppress these (unlike UI-disable-driven suppressions).
            # _tool_server_map: register_name → server_name; canonical reverse map so a
            #   hyphenated server (e.g. chrome-devtools) is resolved correctly (split('-')[0]
            #   is WRONG for it — Q1 caveat).
            self._parked_servers: dict[str, dict] = {}
            self._connecting: dict[str, threading.Event] = {}
            self._suppressed: set[str] = set()
            self._config_disabled: set[str] = set()
            self._tool_server_map: dict[str, str] = {}
            self.loop = asyncio.new_event_loop()
            self.loop_thread = threading.Thread(target=self.start_loop, daemon=True)
            self.loop_thread.start()

            # A fallback way to terminate MCP tool processes after AgentCascade exits
            self.processes = []
            self.monkey_patch_mcp_create_platform_compatible_process()

    def monkey_patch_mcp_create_platform_compatible_process(self):
        try:
            import mcp.client.stdio
            target = mcp.client.stdio._create_platform_compatible_process
        except (ModuleNotFoundError, AttributeError) as e:
            print(e)
            raise ImportError('AgentCascade needs to monkey patch MCP for process cleanup. '
                              'Please upgrade MCP to a higher version with `pip install -U mcp`.') from e

        async def _monkey_patched_create_platform_compatible_process(*args, **kwargs):
            process = await target(*args, **kwargs)
            self.processes.append(process)
            return process

        mcp.client.stdio._create_platform_compatible_process = _monkey_patched_create_platform_compatible_process

    def start_loop(self):
        asyncio.set_event_loop(self.loop)

        # Set a global exception handler to silently handle cross-task exceptions from MCP SSE connections
        def exception_handler(loop, context):
            exception = context.get('exception')
            if exception:
                # Silently handle cross-task exceptions from MCP SSE connections
                if (isinstance(exception, RuntimeError) and
                        'Attempted to exit cancel scope in a different task' in str(exception)):
                    logger.debug('Silently ignoring MCP cross-task exception: %s', str(exception))
                    return  # Silently ignore this type of exception
                if (isinstance(exception, BaseExceptionGroup) and  # noqa
                        'Attempted to exit cancel scope in a different task' in str(exception)):  # noqa
                    logger.debug('Silently ignoring MCP cross-task exception: %s', str(exception))
                    return  # Silently ignore this type of exception

            # Other exceptions are handled normally
            loop.default_exception_handler(context)

        self.loop.set_exception_handler(exception_handler)
        self.loop.run_forever()

    def is_valid_mcp_servers(self, config: dict):
        """Example of mcp servers configuration:
        {
         "mcpServers": {
            "memory": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-memory"]
            },
            "filesystem": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/allowed/files"]
            }
         }
        }
        """

        # Check if the top-level key "mcpServers" exists and its value is a dictionary
        if not isinstance(config, dict) or 'mcpServers' not in config or not isinstance(config['mcpServers'], dict):
            return False
        mcp_servers = config['mcpServers']
        # Check each sub-item under "mcpServers"
        for key in mcp_servers:
            server = mcp_servers[key]
            # Each sub-item must be a dictionary
            if not isinstance(server, dict):
                return False
            if 'command' in server:
                # "command" must be a string
                if not isinstance(server['command'], str):
                    return False
                # "args" must be a list
                if 'args' not in server or not isinstance(server['args'], list):
                    return False
            if 'url' in server:
                # "url" must be a string
                if not isinstance(server['url'], str):
                    return False
                # "headers" must be a dictionary
                if 'headers' in server and not isinstance(server['headers'], dict):
                    return False
            # If the "env" key exists, it must be a dictionary
            if 'env' in server and not isinstance(server['env'], dict):
                return False
            # "enabled" is optional; when present it must be a boolean (defaults to true when absent)
            if 'enabled' in server and not isinstance(server['enabled'], bool):
                return False
        return True

    def initConfig(self, config: Dict):
        if not self.is_valid_mcp_servers(config):
            raise ValueError('Config of mcpservers is not valid')
        logger.info(f"Initializing MCP tools from mcp servers: {list(config['mcpServers'].keys())}")
        # Submit coroutine to the event loop and wait for the result
        future = asyncio.run_coroutine_threadsafe(self.init_config_async(config), self.loop)
        try:
            # BUG_0310 R1: bounded wait — MCP_CONNECT_TIMEOUT_S caps the boot thread so a
            # dead/slow npx can no longer hang startup indefinitely.
            result = future.result(timeout=MCP_CONNECT_TIMEOUT_S)
            return result
        except concurrent.futures.TimeoutError as e:
            # concurrent.futures.TimeoutError IS the builtin TimeoutError (3.11+), so this
            # branch also catches a worker that raised TimeoutError itself (result() re-raises
            # the task's exception). A still-PENDING future => our bounded wait expired; a DONE
            # future => the worker failed. Name the cause accurately (see memory
            # executor-deadline-timeouterror-and-daemon-worker-gotchas).
            if future.done():
                logger.error(f'Failed in initializing MCP tools: {e}')
            else:
                logger.error('[MCP] Eager initialization TIMED OUT after %.1fs waiting for the '
                             'MCP connect/handshake — no MCP tools loaded this boot. Raise '
                             'AGENT_CASCADE_MCP_CONNECT_TIMEOUT_S if the server is legitimately '
                             'slow, or disable it: %s',
                             MCP_CONNECT_TIMEOUT_S, list(config['mcpServers'].keys()))
                # BUG_0310 review: cancel the coroutine we abandoned instead of letting it
                # finish later — a slow-but-successful connect would otherwise spawn a live
                # MCP subprocess + populate self.clients while we discard the tools
                # (connected-but-unregistered). .cancel() requests CancelledError on the
                # asyncio task via the loop. Best-effort: a cancel failure must NOT turn the
                # bounded-timeout path into a new crash (it self-heals via the idempotency
                # guard on the next config re-push anyway).
                try:
                    future.cancel()
                except Exception as cancel_err:  # noqa: BLE001 — never mask the timeout
                    logger.debug('[MCP] Could not cancel the timed-out init task (%s); it will '
                                 'self-heal on the next config re-push via the idempotency guard.',
                                 cancel_err)
            raise e
        except Exception as e:
            logger.error(f'Failed in initializing MCP tools: {e}')
            raise e

    async def init_config_async(self, config: Dict):
        tools: list = []
        mcp_servers = config['mcpServers']
        for server_name in mcp_servers:
            server = mcp_servers[server_name]
            # Skip disabled servers — the 'enabled' flag is a UI concern persisted to
            # config/mcp_servers.json; it defaults to True when absent. This is the single
            # connection entry point, so the check also guards any future initConfig caller.
            if not (isinstance(server, dict) and server.get('enabled', True)):
                logger.debug(f"MCP server '{server_name}' is disabled — skipping.")
                continue
            # Strip 'enabled' — it is a UI concern, not a connection parameter.
            # The fingerprint must match what connect_server computes so toggles
            # do not produce duplicate connections.
            conn_config = {k: v for k, v in server.items() if k != 'enabled'}
            _cfg_fingerprint = hashlib.md5(
                json.dumps(conn_config, sort_keys=True, default=str).encode()
            ).hexdigest()[:8]
            _guard_key = f"{server_name}:{_cfg_fingerprint}"

            # Idempotency guard: return cached tools for already-connected servers
            # (prevents process explosion when initConfig is called multiple times,
            # e.g. on repeated UI config updates, while still allowing new agents
            # to get tool registrations).  Uses a prefix scan so the guard is
            # robust against hash differences from a prior connect_server call.
            with self._mcp_lock:
                if self._find_connected_key(server_name) is not None:
                    logger.debug(f"MCP server '{server_name}' already connected, "
                                 f"returning cached tools.")
                    tools.extend(self._server_tools.get(server_name, []))
                    continue

            client = MCPClient()
            await client.connection_server(mcp_server_name=server_name,
                                           mcp_server=conn_config)

            client_id = server_name + '_' + str(
                uuid.uuid4())  # To allow the same server name be used across different running agents
            client.client_id = client_id  # Ensure client_id is set on the client instance
            self.clients[client_id] = client  # Add to clients dict after successful connection
            # Create tool classes for this client (shared implementation with connect_server).
            server_tools = await self._build_tools_for_client(client, server_name, client_id)
            tools.extend(server_tools)

            # Cache this server's tools and mark as connected (only after all tool creation
            # succeeded — if an exception occurred above, the guard stays unset so retries work).
            with self._mcp_lock:
                self._server_tools[server_name] = server_tools
                self._connected_servers[_guard_key] = client_id

        return tools

    def create_tool_class(self, register_name, register_client_id, tool_name, tool_desc, tool_parameters):

        # Track this MCP tool name globally for security filtering
        self._registered_tool_names.add(register_name)

        class ToolClass(BaseTool):
            name = register_name
            description = tool_desc
            parameters = tool_parameters
            client_id = register_client_id

            def call(self, params: Union[str, dict], **kwargs) -> str:
                if isinstance(params, (str, bytes)):
                    tool_args = json.loads(params)
                elif isinstance(params, dict):
                    tool_args = params
                else:
                    tool_args = json.loads(params)
                # Submit coroutine to the event loop and wait for the result
                manager = MCPManager()
                try:
                    client = manager.clients[self.client_id]
                except KeyError:
                    return 'Error: MCP server disconnected — this tool is no longer available.'
                # Pass agent context so media saves can queue a path notification
                agent_instance_name = kwargs.get('agent_instance_name')
                agent_obj = kwargs.get('agent_obj')
                pool = getattr(agent_obj, 'pool', None) if agent_obj else None
                future = asyncio.run_coroutine_threadsafe(
                    client.execute_function(tool_name, tool_args,
                                             pool=pool, instance_name=agent_instance_name),
                    manager.loop)
                try:
                    result = future.result()
                    return result
                except Exception as e:
                    logger.error(f'Failed in executing MCP tool: {e}')
                    raise e

        ToolClass.__name__ = f'{register_name}_Class'
        return ToolClass()

    def _find_connected_key(self, server_name) -> str | None:
        """Find the guard key for a server name in _connected_servers.

        _connected_servers is keyed "server_name:config_hash", so this matches on the
        "{name}:" prefix — the config hash is an internal detail that may differ from
        what connect_server would compute for the same server. Must be called under _mcp_lock.
        """
        for key in self._connected_servers:
            if key.startswith(f'{server_name}:'):
                return key
        return None

    async def _build_tools_for_client(self, client, server_name, client_id) -> list:
        """Create tool classes for a connected MCP client.

        Returns the full tool list for one server: the server's own tools (with
        inputSchema reduced to the OpenAI function-schema subset), plus list_resources
        and read_resource when the client exposes resources. Must be awaited inside the
        MCP event loop; connect_server bridges to it via asyncio.run_coroutine_threadsafe.
        """
        tools: list = []
        for tool in client.tools:
            # MCP tool inputSchema example:
            #   {"name": "read_query", "description": "...",
            #    "inputSchema": {"type": "object", "properties": {...}, "required": [...]}}
            parameters = tool.inputSchema
            # The 'required' field in inputSchema may be absent and needs to be initialized.
            if 'required' not in parameters:
                parameters['required'] = []
            # Keep only the fields that conform to the standard OpenAI function schema.
            required_fields = {'type', 'properties', 'required'}
            missing_fields = required_fields - parameters.keys()
            if missing_fields:
                raise ValueError(f'Missing required fields in schema: {missing_fields}')
            cleaned_parameters = {
                'type': parameters['type'],
                'properties': parameters['properties'],
                'required': parameters['required'],
            }
            register_name = server_name + '-' + tool.name
            agent_tool = self.create_tool_class(
                register_name=register_name,
                register_client_id=client_id,
                tool_name=tool.name,
                tool_desc=tool.description,
                tool_parameters=cleaned_parameters)
            tools.append(agent_tool)

        if client.resources:
            # List resources
            list_resources_tool_name = server_name + '-list_resources'
            list_resources_params = {'type': 'object', 'properties': {}, 'required': []}
            list_resources_agent_tool = self.create_tool_class(
                register_name=list_resources_tool_name,
                register_client_id=client_id,
                tool_name='list_resources',
                tool_desc=(
                    'Servers expose a list of concrete resources through this tool. '
                    'By invoking it, you can discover the available resources and obtain '
                    'resource templates, which help clients understand how to construct '
                    'valid URIs. These URI formats will be used as input parameters for '
                    'the read_resource function. '),
                tool_parameters=list_resources_params)
            tools.append(list_resources_agent_tool)

            # Read resource — augment its description with any resource templates the server exposes.
            resources_template_str = ''
            try:
                list_resource_templates = await client.session.list_resource_templates()
                if list_resource_templates.resourceTemplates:
                    resources_template_str = '\n'.join(
                        str(template) for template in list_resource_templates.resourceTemplates)
            except Exception as e:
                logger.warning(f'Failed in listing MCP resource templates: {e}')

            read_resource_tool_name = server_name + '-read_resource'
            read_resource_params = {
                'type': 'object',
                'properties': {
                    'uri': {
                        'type': 'string',
                        'description': 'The URI identifying the specific resource to access'
                    }
                },
                'required': ['uri']
            }
            original_tool_desc = (
                'Request to access a resource provided by a connected MCP server. '
                'Resources represent data sources that can be used as context, such as '
                'files, API responses, or system information.')
            if resources_template_str:
                tool_desc = original_tool_desc + '\nResource Templates:\n' + resources_template_str
            else:
                tool_desc = original_tool_desc
            read_resource_agent_tool = self.create_tool_class(
                register_name=read_resource_tool_name,
                register_client_id=client_id,
                tool_name='read_resource',
                tool_desc=tool_desc,
                tool_parameters=read_resource_params)
            tools.append(read_resource_agent_tool)

        return tools

    def disconnect_server(self, server_name: str, templates: list) -> bool:
        """Disconnect one MCP server: remove its tools from agent templates, clean up
        the client process, and clear all tracking state.  Idempotent: returns True
        when the server is not connected (already in the desired state)."""
        with self._mcp_lock:
            guard_key = self._find_connected_key(server_name)
            if guard_key is None:
                return True  # not connected — already disabled

            client_id = self._connected_servers[guard_key]
            tools = self._server_tools.get(server_name, [])
            client = self.clients.get(client_id)

            # Remove tools from all templates' function_map (stops NEW tool offers).
            for template in templates:
                for tool in tools:
                    if tool.name in template.function_map:
                        del template.function_map[tool.name]

            # Pop all tracking state inside the same lock so connect/status never
            # see a half-disconnected server.
            self.clients.pop(client_id, None)
            self._connected_servers.pop(guard_key, None)
            self._server_tools.pop(server_name, None)
            # Remove this server's exact tool names from the global security set
            # (prefix matching would collide across hyphenated server names).
            exact_names = {t.name for t in tools}
            self._registered_tool_names -= exact_names

        # Clean up the client outside the lock (async, may block).
        if client:
            try:
                asyncio.run_coroutine_threadsafe(
                    client.cleanup(), self.loop).result(timeout=10)
            except Exception as e:
                logger.warning(
                    "MCP client cleanup for '%s' failed (may already be dead): %s",
                    server_name, e)

        logger.info("MCP server '%s' disconnected (%d tools removed).",
                    server_name, len(tools))
        return True

    def connect_server(self, server_name: str, server_config: dict,
                       templates: list, timeout_s: Optional[float] = None) -> bool:
        """Connect one MCP server and register its tools with all agent templates.
        Returns True on success (including if already connected), False on failure.
        timeout_s bounds BOTH the connect handshake and the tools/list step; None → 30 s
        (the value hardcoded before parameterization, so the UI toggle path is unchanged)."""
        _step_timeout = timeout_s if timeout_s is not None else 30.0
        # Strip 'enabled' before hashing — it is a UI concern, not a connection
        # parameter, so the fingerprint stays stable across toggles.
        conn_config, _cfg_fingerprint = self._conn_config_and_hash(server_config)
        _guard_key = f'{server_name}:{_cfg_fingerprint}'

        # Already connected?  Use a prefix scan so a hash mismatch from a
        # startup that included 'enabled' in the config does not cause a
        # duplicate connection.
        with self._mcp_lock:
            if self._find_connected_key(server_name) is not None:
                return True

        client = MCPClient()
        try:
            asyncio.run_coroutine_threadsafe(
                client.connection_server(
                    mcp_server_name=server_name, mcp_server=conn_config),
                self.loop).result(timeout=_step_timeout)
        except Exception as e:
            logger.error("Failed to connect MCP server '%s': %s", server_name, e)
            return False

        client_id = server_name + '_' + str(uuid.uuid4())
        client.client_id = client_id

        # Create tool classes (shared with init_config_async). The helper runs in the
        # MCP event loop; bridge the coroutine from this calling thread.
        try:
            new_tools = asyncio.run_coroutine_threadsafe(
                self._build_tools_for_client(client, server_name, client_id),
                self.loop).result(timeout=_step_timeout)
        except Exception as e:
            logger.error(
                "Failed to create tools for MCP server '%s': %s", server_name, e)
            try:
                asyncio.run_coroutine_threadsafe(
                    client.cleanup(), self.loop).result(timeout=10)
            except Exception:
                pass
            return False

        # Register tools in all templates. A lazy stub may already occupy the slot
        # (register_stubs parked this server with placeholder schema); the LIVE tool MUST
        # overwrite it — that is the whole point of the on-use connect. Any other
        # pre-existing (non-stub) entry is left untouched to preserve the original
        # no-clobbering behavior for name collisions with core tools / another server.
        for template in templates:
            for tool in new_tools:
                existing = template.function_map.get(tool.name)
                if existing is None or getattr(existing, '_mcp_stub', False):
                    template.function_map[tool.name] = tool

        with self._mcp_lock:
            self.clients[client_id] = client
            self._connected_servers[_guard_key] = client_id
            self._server_tools[server_name] = new_tools
            # Keep the canonical name→server reverse map authoritative for live tools too
            # (stubs populated it; this corrects it whether the server was parked-then-
            # connected or connected directly).
            for tool in new_tools:
                self._tool_server_map[tool.name] = server_name

        logger.info("MCP server '%s' connected (%d tools registered).",
                    server_name, len(new_tools))
        return True

    # ── Lazy / on-use connect (BUG_0310 follow-up) ──────────────────────────────
    # All helpers below must NOT be called with _mcp_lock already held (they acquire it
    # themselves; _mcp_lock is a plain threading.Lock — non-reentrant).

    def is_parked(self, server_name: str) -> bool:
        """True iff server_name is registered but NOT connected and NOT mid-connect — i.e.
        a genuine candidate for a lazy on-use connect."""
        with self._mcp_lock:
            return (server_name in self._parked_servers
                    and self._find_connected_key(server_name) is None
                    and server_name not in self._connecting)

    def is_suppressed(self, server_name: str) -> bool:
        """True iff the server is turned off (all tools disabled for every agent, or config
        enabled:false). ensure_connected refuses suppressed servers."""
        with self._mcp_lock:
            return server_name in self._suppressed

    def _is_connected(self, server_name: str) -> bool:
        with self._mcp_lock:
            return self._find_connected_key(server_name) is not None

    def server_for_tool(self, tool_name: str) -> Optional[str]:
        """Resolve the server that owns a registered MCP tool. Uses the canonical
        _tool_server_map (a name like 'chrome-devtools-click' must NOT be split on '-', so
        split('-')[0] would wrongly yield 'chrome'). Falls back to the legacy prefix split
        only when the map has no entry (a tool registered on a path that predates lazy mode)."""
        with self._mcp_lock:
            srv = self._tool_server_map.get(tool_name)
        if srv is not None:
            return srv
        return tool_name.split('-')[0] if '-' in tool_name else None

    @staticmethod
    def _conn_config_and_hash(server: dict) -> tuple:
        """(conn_config, 8-char fingerprint) — strips 'enabled' so the hash is stable across
        UI toggles. Mirrors the hashing already done in init_config_async / connect_server."""
        conn_config = {k: v for k, v in server.items() if k != 'enabled'}
        h = hashlib.md5(
            json.dumps(conn_config, sort_keys=True, default=str).encode()
        ).hexdigest()[:8]
        return conn_config, h

    def _load_tools_cache(self) -> dict:
        """Read config/mcp_tools_cache.json (lazy tool-metadata cache). {} if absent/bad."""
        try:
            text = MCP_TOOLS_CACHE_PATH.read_text(encoding='utf-8')
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:  # noqa: BLE001 — a corrupt cache must never break boot
            logger.warning('[MCP-lazy] could not parse %s (%s); treating as cold.',
                           MCP_TOOLS_CACHE_PATH, e)
            return {}

    def _save_tools_cache(self, cache: dict) -> None:
        """Persist the tool-metadata cache (best-effort; boot/interrogation must not crash on
        a write error — a cold cache just means tools resurface after the next refresh)."""
        try:
            MCP_TOOLS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = MCP_TOOLS_CACHE_PATH.with_suffix('.tmp')
            tmp.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding='utf-8')
            tmp.replace(MCP_TOOLS_CACHE_PATH)  # atomic-ish on same volume
        except Exception as e:  # noqa: BLE001
            logger.warning('[MCP-lazy] could not write %s: %s', MCP_TOOLS_CACHE_PATH, e)

    def create_stub_tool_class(self, register_name: str, tool_desc: str,
                               tool_parameters: dict):
        """Build a STUB tool instance for a parked server (lazy mode). Mirrors create_tool_class
        but carries no live client: real schema (so tool_search can rank/catalog it) + a sentinel
        client_id so a direct call hits the existing graceful 'MCP server disconnected' path.
        In practice the dispatcher pre-flight connects + OVERWRITES the stub before _call_tool,
        so the call path is only reached if a connect failed (best-effort degrade). Adds the name
        to the deferred-universe set (_registered_tool_names → is_mcp_tool)."""
        self._registered_tool_names.add(register_name)

        class _StubTool(BaseTool):
            _mcp_stub = True
            name = register_name
            description = tool_desc
            parameters = tool_parameters
            client_id = None

            def call(self, params, **kwargs) -> str:
                return ('Error: MCP server disconnected — this tool is not currently '
                        'available. (lazy server not connected)')

        _StubTool.__name__ = f'{register_name}_Stub'
        return _StubTool()

    def register_stubs(self, server_name: str, conn_config: dict, tools: list,
                       templates: list) -> int:
        """Materialize stub tool objects for a parked server so its tools stay discoverable
        (deferred universe = function_map ∩ is_mcp_tool) WITHOUT a live connection (Q8).
        `tools` is a list of {'name' (full register_name), 'description', 'parameters'} in the
        cleaned {type,properties,required} schema shape (H4 — raw MCP inputSchema would yield
        empty candidate docs). Idempotent; NEVER overwrites a live (non-stub) tool. Returns the
        number of stubs newly registered."""
        registered = 0
        for meta in tools:
            register_name = meta.get('name')
            if not register_name:
                continue
            parameters = meta.get('parameters') or {'type': 'object', 'properties': {}, 'required': []}
            stub = self.create_stub_tool_class(register_name, meta.get('description', ''), parameters)
            with self._mcp_lock:
                self._tool_server_map[register_name] = server_name
            for template in templates:
                existing = getattr(template, 'function_map', {}).get(register_name)
                if existing is not None and not getattr(existing, '_mcp_stub', False):
                    continue  # live (or foreign) tool present — leave it alone
                template.function_map[register_name] = stub
            registered += 1
        return registered

    def init_lazy(self, mcp_servers: dict, templates: list) -> dict:
        """Lazy boot registration (BUG_0310, directive #1). Park every server, register STUBS
        from the metadata cache, connect NOTHING — so the synchronous `mcp_connect` boot phase
        measures ≈0. Per server:
          · enabled:false → hard-disabled: added to _config_disabled + _suppressed, NO stubs.
          · enabled + cache fresh (config_hash matches + has tools) → register stubs, park.
          · enabled + cache cold/stale → park (still connectable by config); collect for
            background interrogation (if enabled) which registers stubs once metadata lands.
        Rebuilds the lazy registries (runtime UI suppressions re-derive via set_ui_disabled_tools).
        Returns {'registered': int, 'parked': int, 'cold': [server names]}."""
        cache = self._load_tools_cache()
        cold: List[tuple] = []
        registered = 0
        with self._mcp_lock:
            self._parked_servers = {}
            self._config_disabled = set()
            self._suppressed = set()  # runtime suppressions re-derive on next set_ui_disabled_tools
            self._tool_server_map.clear()
        for server_name, server in mcp_servers.items():
            if not isinstance(server, dict):
                continue
            conn_config, cfg_hash = self._conn_config_and_hash(server)
            if not server.get('enabled', True):
                with self._mcp_lock:
                    self._config_disabled.add(server_name)
                    self._suppressed.add(server_name)
                logger.info("[MCP-lazy] '%s' disabled in config — parked OFF (no stubs).",
                            server_name)
                continue
            with self._mcp_lock:
                self._parked_servers[server_name] = conn_config
            cached = cache.get(server_name)
            if cached and cached.get('config_hash') == cfg_hash and cached.get('tools'):
                registered += self.register_stubs(server_name, conn_config, cached['tools'], templates)
            else:
                cold.append((server_name, conn_config, cfg_hash))
        if cold:
            if _lazy_settings().MCP_LAZY_COLD_INTERROGATE:
                self.refresh_tools_cache_background(cold, templates)
            else:
                logger.info('[MCP-lazy] cold-interrogation disabled — %d server(s) park without '
                            'stubs until next cold refresh/boot: %s',
                            len(cold), [s[0] for s in cold])
        logger.info('[MCP-lazy] boot parked %d server(s) (stubs=%d, cold=%d, connected=0).',
                    len(self._parked_servers), registered, len(cold))
        return {'registered': registered, 'parked': len(self._parked_servers),
                'cold': [s[0] for s in cold]}

    def _interrogate_one(self, server_name: str, conn_config: dict, cfg_hash: str,
                         templates: list) -> Optional[list]:
        """Cold-cache harvest (Q8): return the live tool objects for server_name (for caching),
        or None if it could not be interrogated right now (launch failure, or another connect
        owns it — neither is worth fighting; the caller skips and it is retried next cold
        refresh). Parks the server back if THIS call connected it (Q2: parked = schema known,
        no live client); leaves it connected if a user already connected it (we just harvest).
        Holds the per-server _connecting Event for the WHOLE connect→list→park window so a
        concurrent ensure_connected waits (and re-connects after we park) instead of spawning a
        second subprocess. Returns None and logs loudly on launch failure (a server that cannot
        start is NOT wrongly marked connected and NOT stubbed)."""
        claim = threading.Event()
        with self._mcp_lock:
            if self._find_connected_key(server_name) is not None:
                return list(self._server_tools.get(server_name, []))  # user live; harvest
            if self._connecting.get(server_name) is not None:
                return None  # another connect owns it right now; skip
            self._connecting[server_name] = claim
        try:
            if not self.connect_server(server_name, conn_config, templates,
                                       timeout_s=_lazy_settings().MCP_LAZY_CONNECT_TIMEOUT_S):
                logger.warning("[MCP-lazy] cold interrogation could not connect '%s' — leaving it "
                               'unregistered (retried next cold refresh/boot).', server_name)
                return None
            with self._mcp_lock:
                tools = list(self._server_tools.get(server_name, []))
            self.disconnect_server(server_name, templates)  # Q2: park back after interrogation
            return tools
        finally:
            with self._mcp_lock:
                self._connecting.pop(server_name, None)
            claim.set()  # wake any waiter that raced us (they re-check + take over)

    def refresh_tools_cache_background(self, cold_servers: list, templates: list) -> None:
        """Spawn ONE bounded one-shot daemon thread to populate the tool-metadata cache for
        cold/stale servers (connect→tools/list→disconnect, Q8), then register stubs. Runs OFF
        the boot thread so the synchronous `mcp_connect` phase stays ≈0. Sequential (not
        parallel) so a cold boot never multi-spawns npx/uvx children at once. Mirrors the
        proven rescan_vaults_background one-shot daemon precedent. Best-effort: one server's
        failure logs + moves on; the thread is daemon so it cannot block process exit."""
        if not cold_servers:
            return

        def _run():
            cache = self._load_tools_cache()
            wrote = False
            for server_name, conn_config, cfg_hash in cold_servers:
                try:
                    tools = self._interrogate_one(server_name, conn_config, cfg_hash, templates)
                    if tools is None:
                        continue
                    cache[server_name] = {
                        'config_hash': cfg_hash,
                        'tools': [{'name': t.name, 'description': t.description,
                                   'parameters': t.parameters} for t in tools],
                    }
                    self.register_stubs(server_name, conn_config,
                                        cache[server_name]['tools'], templates)
                    wrote = True
                except Exception as e:  # noqa: BLE001 — one server must not abort the others
                    logger.warning("[MCP-lazy] cold interrogation for '%s' failed: %s",
                                   server_name, e)
            if wrote:
                self._save_tools_cache(cache)

        t = threading.Thread(target=_run, name='mcp-lazy-cold-cache', daemon=True)
        try:
            t.start()
        except Exception as e:  # noqa: BLE001 — thread-spawn failure must not break boot
            logger.debug('[MCP-lazy] could not spawn cold-cache thread: %s', e)

    def _server_tool_names(self, server_name: str) -> set:
        """Union of every tool name attributed to a server: stubs (via _tool_server_map) ∪
        live tools (via _server_tools) — correct whether the server is parked or connected."""
        with self._mcp_lock:
            from_map = {n for n, s in self._tool_server_map.items() if s == server_name}
            live = {t.name for t in self._server_tools.get(server_name, [])}
        return from_map | live

    def _server_fully_disabled(self, server_name: str, pool, templates_map: dict) -> bool:
        """True iff EVERY tool of server_name is disabled for EVERY agent template — either
        individually (all its tools in that agent's disabled set) or via the __all_mcp_tools__
        sentinel. Only then is a live server pure idle cost. Uses the UI-cache resolver
        (get_ui_disabled_tools_for_agent) — v1 scope per Q5/H3: template-config/baseline
        disables are a separate v2 hardening. Conservative: an empty tool set or an
        unavailable resolver returns False (never suppress on uncertainty)."""
        names = self._server_tool_names(server_name)
        if not names:
            return False
        get = getattr(pool, 'get_ui_disabled_tools_for_agent', None)
        if not callable(get):
            return False
        for agent_name, tpl in templates_map.items():
            agent_type = getattr(tpl, 'agent_type', '') or ''
            disabled = get(agent_name, agent_type) or set()
            if '__all_mcp_tools__' in disabled:
                continue
            if names <= disabled:
                continue
            return False  # this agent can still use at least one of the server's tools
        return True

    def _re_park_server(self, server_name: str, templates: list) -> None:
        """Restore a previously-suppressed server to parked-with-stubs (re-enable path in the
        off-hook): reads its conn_config from _parked_servers and its cached schema, and
        re-registers stubs (the earlier disconnect dropped them). No-op if config/cache missing."""
        with self._mcp_lock:
            conn_config = self._parked_servers.get(server_name)
        if conn_config is None:
            return
        entry = self._load_tools_cache().get(server_name)
        if entry and entry.get('tools'):
            self.register_stubs(server_name, conn_config, entry['tools'], templates)

    def apply_ui_disabled(self, pool) -> None:
        """Recompute per-server all-disabled→off state after a UI disabled_tools change
        (BUG_0310 directive #3). A server is turned off (suppressed + disconnected if live) iff
        every one of its tools is disabled for every agent template (or __all_mcp_tools__ is
        active for every template). A suppressed server no longer fully disabled is restored to
        parked (stubs re-registered) so it can connect on next use. Config-level enabled:false
        servers stay permanently suppressed. MUST NOT be called with _mcp_lock held."""
        if pool is None:
            return
        templates_map = getattr(pool, 'templates', None) or {}
        templates = list(templates_map.values())
        with self._mcp_lock:
            servers = (set(self._parked_servers)
                       | {k.split(':', 1)[0] for k in self._connected_servers}
                       | set(self._suppressed))
            config_disabled = set(self._config_disabled)
        for server in servers:
            if server in config_disabled:
                continue  # permanent off; tool toggles never revive a config-disabled server
            fully = self._server_fully_disabled(server, pool, templates_map)
            with self._mcp_lock:
                suppressed = server in self._suppressed
            if fully and not suppressed:
                if self._is_connected(server):
                    self.disconnect_server(server, templates)
                with self._mcp_lock:
                    self._suppressed.add(server)
                logger.info("[MCP-off] '%s': all tools disabled for every agent — turned off.",
                            server)
            elif not fully and suppressed:
                with self._mcp_lock:
                    self._suppressed.discard(server)
                self._re_park_server(server, templates)
                logger.info("[MCP-on] '%s': re-enabled — restored to parked.", server)

    def ensure_connected(self, server_name: str, pool=None,
                         timeout_s: Optional[float] = None) -> bool:
        """Connect a parked MCP server on first use (lazy mode, directive #2). Single-flight per
        server (Q4): the first caller connects; concurrent callers WAIT on the owner's
        threading.Event (NOT on _mcp_lock, which is a plain Lock with no wait/notify) rather
        than each spawning a subprocess. Honours _suppressed. Bounded + best-effort: NEVER
        raises — returns False on failure so the caller degrades to the stub's graceful
        'MCP server disconnected' error. A waiter that finds the owner re-parked the server
        (cold interrogation) takes over rather than returning False.
        MUST NOT be called with _mcp_lock already held."""
        if timeout_s is None:
            timeout_s = _lazy_settings().MCP_LAZY_CONNECT_TIMEOUT_S
        templates = list(pool.templates.values()) if pool is not None else []
        for _attempt in range(2):
            with self._mcp_lock:
                if self._find_connected_key(server_name) is not None:
                    return True
                if server_name in self._suppressed:
                    logger.debug('[MCP-lazy] %r suppressed — not connecting.', server_name)
                    return False
                cfg = self._parked_servers.get(server_name)
                if cfg is None:
                    logger.warning('[MCP-lazy] %r is not parked — cannot lazy-connect.', server_name)
                    return False
                ev = self._connecting.get(server_name)
                if ev is not None:
                    owner = False
                else:
                    claim = threading.Event()
                    self._connecting[server_name] = claim
                    owner = True
            if not owner:
                # Owner (a user connect OR the cold interrogation) is in flight; wait it out,
                # then re-check on the next iteration (they may have parked it back).
                ev.wait(2 * timeout_s + _MCP_CONNECT_TEARDOWN_MARGIN_S)
                continue
            # Owner performs the connect OUTSIDE the lock (connect_server acquires it itself).
            ok = False
            try:
                ok = self.connect_server(server_name, cfg, templates, timeout_s=timeout_s)
            except Exception as e:  # noqa: BLE001 — best-effort lazy connect, never propagate
                logger.error("[MCP-lazy] connect for '%s' raised: %s", server_name, e)
                ok = False
            finally:
                with self._mcp_lock:
                    self._connecting.pop(server_name, None)
                claim.set()  # wake waiters on both success and failure
            return ok
        # Exhausted retries (a cold interrogation re-parked it mid-wait and a third party holds
        # the guard again) — best-effort: degrade to the stub error rather than looping forever.
        return self._is_connected(server_name)

    def ensure_connected_async(self, server_names, pool) -> None:
        """Fire-and-forget lazy pre-warm for the tool_search unlock trigger (Q1 primary).
        Runs the (bounded, single-flight) connects on ONE one-shot daemon thread so tool_search
        never blocks on connect latency nor is DMS-vulnerable to it. Best-effort; exceptions
        swallowed (the dispatcher pre-flight is the correctness backstop)."""
        names = [s for s in dict.fromkeys(server_names) if s]  # de-dup, preserve order
        if not names or pool is None:
            return

        def _run():
            for s in names:
                try:
                    if self.is_parked(s):  # already connected/connecting/suppressed → skip
                        self.ensure_connected(s, pool=pool)
                except Exception as e:  # noqa: BLE001 — pre-warm is best-effort
                    logger.debug('[MCP-lazy] background pre-warm of %r failed: %s', s, e)

        t = threading.Thread(target=_run, name='mcp-lazy-prewarm', daemon=True)
        try:
            t.start()
        except Exception as e:  # noqa: BLE001 — thread-spawn failure must not break tool_search
            logger.debug('[MCP-lazy] could not spawn pre-warm thread: %s', e)

    def shutdown(self):
        futures = []
        for client_id in list(self.clients.keys()):
            client: MCPClient = self.clients[client_id]
            future = asyncio.run_coroutine_threadsafe(client.cleanup(), self.loop)
            futures.append(future)
            del self.clients[client_id]
        with self._mcp_lock:
            self._connected_servers.clear()
            self._server_tools.clear()
            # Lazy-connect state — clear so a reboot re-derives cleanly from config/cache.
            self._parked_servers.clear()
            self._connecting.clear()
            self._tool_server_map.clear()
            self._suppressed.clear()
            self._config_disabled.clear()
        time.sleep(1)  # Wait for the graceful cleanups, otherwise fall back

        # fallback
        if asyncio.all_tasks(self.loop):
            logger.info(
                'There are still tasks in `MCPManager().loop`, force terminating the MCP tool processes. There may be some exceptions.'
            )
            for process in self.processes:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass  # it's ok, the process may exit earlier

        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop_thread.join()


def _ext_from_mime(mime: Optional[str], media_type: str) -> Optional[str]:
    """Map an MCP mimeType to a media extension hint.

    Returns the MIME subtype (text after '/') lowercased, e.g. 'image/png' -> 'png',
    'audio/wav' -> 'wav'. Returns None when there is no usable MIME so that
    save_media_to_media applies its per-type default (jpg/mp3). Safe because
    save_media_to_media only honours the hint when it is a recognised extension
    and sanitizes everything else, so a malformed MIME can never yield an
    arbitrary or extensionless filename.
    """
    if not mime or '/' not in mime:
        return None
    return mime.rsplit('/', 1)[-1].lower()


def _queue_media_path_notification(tool_name: str, media_type: str, path: str,
                                   pool=None, instance_name: Optional[str] = None) -> None:
    """Queue a system notification telling the agent where an MCP media file was saved.

    Mirrors the 'Generated image: <path> (...)' feedback that media_gen provides,
    so the agent always knows the on-disk location of internal media copies.
    For images, includes dimensions (WxH) so the agent can use crop_region to zoom.
    Best-effort: silently no-ops if pool or instance_name is unavailable.
    """
    if not pool or not instance_name:
        return
    try:
        from agent_cascade.operation_manager import _queue_tool_warning
        # Include image dimensions for zoom/crop reference
        detail = ''
        if media_type == 'image':
            try:
                from PIL import Image
                with Image.open(path) as img:
                    w, h = img.size
                detail = f" ({w}x{h})"
            except Exception as e:
                logger.debug('[MCP_MEDIA_NOTIFY] dimension read failed for %s: %s', path, e)
        msg = f"MCP {media_type} saved to: {path}{detail} (tool: {tool_name})"
        _queue_tool_warning(pool, instance_name, msg)
    except Exception as e:
        logger.debug('[MCP_MEDIA_NOTIFY] failed to queue for %s: %s', instance_name, e)


class MCPClient:

    def __init__(self):
        from mcp import ClientSession
        self.session: Optional[ClientSession] = None
        self.tools: list = None
        self.exit_stack = AsyncExitStack()
        self.resources: bool = False
        self._last_mcp_server_name = None
        self._last_mcp_server = None
        self.client_id = None  # For replacing in MCPManager.clients

    async def connection_server(self, mcp_server_name, mcp_server):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.sse import sse_client
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamablehttp_client
        """Connect to an MCP server and retrieve the available tools."""
        # Save parameters
        self._last_mcp_server_name = mcp_server_name
        self._last_mcp_server = mcp_server

        try:
            if 'url' in mcp_server:
                url = mcp_server.get('url')
                sse_read_timeout = mcp_server.get('sse_read_timeout', 300)
                logger.info(f'{mcp_server_name} sse_read_timeout: {sse_read_timeout}s')
                if mcp_server.get('type', 'sse') == 'streamable-http':
                    # streamable-http mode
                    """streamable-http mode mcp example:
                    {"mcpServers": {
                            "streamable-mcp-server": {
                            "type": "streamable-http",
                            "url":"http://0.0.0.0:8000/mcp"
                            }
                        }
                    }
                    """
                    headers = mcp_server.get('headers', {})
                    self._streams_context = streamablehttp_client(
                        url=url, headers=headers, sse_read_timeout=datetime.timedelta(seconds=sse_read_timeout))
                    read_stream, write_stream, get_session_id = await self.exit_stack.enter_async_context(
                        self._streams_context)
                    self._session_context = ClientSession(read_stream, write_stream)
                    self.session = await self.exit_stack.enter_async_context(self._session_context)
                else:
                    # sse mode
                    headers = mcp_server.get('headers', {'Accept': 'text/event-stream'})
                    self._streams_context = sse_client(url, headers, sse_read_timeout=sse_read_timeout)
                    streams = await self.exit_stack.enter_async_context(self._streams_context)
                    self._session_context = ClientSession(*streams)
                    self.session = await self.exit_stack.enter_async_context(self._session_context)
            else:
                server_params = StdioServerParameters(command=mcp_server['command'],
                                                      args=mcp_server['args'],
                                                      env=mcp_server.get('env', None))
                stdio_transport = await self.exit_stack.enter_async_context(stdio_client(server_params))
                self.stdio, self.write = stdio_transport
                self.session = await self.exit_stack.enter_async_context(ClientSession(self.stdio, self.write))
                logger.info(
                    f'Initializing a MCP stdio_client, if this takes forever, please check the config of this mcp server: {mcp_server_name}'
                )

            await self.session.initialize()
            list_tools = await self.session.list_tools()
            self.tools = list_tools.tools
            try:
                list_resources = await self.session.list_resources()  # Check if the server has resources
                if list_resources.resources:
                    self.resources = True
            except Exception:
                # logger.info(f"No list resources: {e}")
                pass
        except Exception as e:
            logger.warning(f'Failed in connecting to MCP server: {e}')
            raise e

    async def reconnect(self):
        # Create a new MCPClient and connect
        if self.client_id is None:
            raise RuntimeError(
                'Cannot reconnect: client_id is None. This usually means the client was not properly registered in MCPManager.'
            )
        new_client = MCPClient()
        new_client.client_id = self.client_id
        await new_client.connection_server(self._last_mcp_server_name, self._last_mcp_server)
        return new_client

    async def execute_function(self, tool_name, tool_args: dict,
                               pool=None, instance_name: Optional[str] = None):
        from mcp.types import BlobResourceContents, TextResourceContents

        # Check if session is alive
        try:
            await self.session.send_ping()
        except Exception as e:
            logger.info(f"Session is not alive, please increase 'sse_read_timeout' in the config, try reconnect: {e}")
            # Auto reconnect
            try:
                from agent_cascade.tools.mcp_manager import MCPManager
                manager = MCPManager()
                if self.client_id is not None:
                    manager.clients[self.client_id] = await self.reconnect()
                    return await manager.clients[self.client_id].execute_function(
                        tool_name, tool_args, pool=pool, instance_name=instance_name)
                else:
                    logger.info('Reconnect failed: client_id is None')
                    return 'Session reconnect (client creation) exception: client_id is None'
            except Exception as e3:
                logger.info(f'Reconnect (client creation) exception type: {type(e3)}, value: {repr(e3)}')
                return f'Session reconnect (client creation) exception: {e3}'
        if tool_name == 'list_resources':
            try:
                list_resources = await self.session.list_resources()
                if list_resources.resources:
                    resources_str = '\n\n'.join(str(resource) for resource in list_resources.resources)
                else:
                    resources_str = 'No resources found'
                return resources_str
            except Exception as e:
                logger.info(f'No list resources: {e}')
                return f'Error: {e}'
        elif tool_name == 'read_resource':
            try:
                uri = tool_args.get('uri')
                if not uri:
                    raise ValueError('URI is required for read_resource')
                read_resource = await self.session.read_resource(uri)
                texts = []
                for resource in read_resource.contents:
                    if isinstance(resource, TextResourceContents):
                        texts.append(resource.text)
                    elif isinstance(resource, BlobResourceContents):
                        # Guarded: only persist blobs whose MIME identifies a known media type.
                        mt = (resource.mimeType or '').lower()
                        if mt.startswith(('image/', 'audio/')):
                            media_type = mt.split('/', 1)[0]
                            try:
                                raw = base64.b64decode(resource.blob) if resource.blob else b''
                                if raw:
                                    path = save_media_to_media(
                                        raw, media_type, ext=_ext_from_mime(mt, media_type)
                                    )
                                    texts.append(f'![{media_type}]({path})')
                                    _queue_media_path_notification(
                                        'read_resource', media_type, path,
                                        pool=pool, instance_name=instance_name)
                                    continue
                            except (MediaStorageError, ValueError) as e:
                                logger.warning(f'MCP blob resource persist failed, skipping: {e}')
                        # Non-media blob, empty payload, or persist failure: never dump the
                        # raw base64 blob into context — emit a short marker instead.
                        texts.append(f'[NOTE: skipped non-media blob resource: {resource.uri}]')
                if texts:
                    return '\n\n'.join(texts)
                else:
                    return 'Failed to read resource'
            except Exception as e:
                logger.info(f'Failed to read resource: {e}')
                return f'Error: {e}'
        else:
            response = await self.session.call_tool(tool_name, tool_args)
            texts = []
            for content in response.content:
                if content.type == 'text':
                    texts.append(content.text)
                elif content.type in ('image', 'audio'):
                    media_type = content.type  # 'image' | 'audio'
                    try:
                        raw = base64.b64decode(content.data) if content.data else b''
                        if not raw:
                            raise ValueError('empty media payload')
                        path = save_media_to_media(
                            raw, media_type, ext=_ext_from_mime(content.mimeType, media_type)
                        )
                        texts.append(f'![{media_type}]({path})')
                        # Push file path into the system notification queue so the agent
                        # knows where the internal copy lives (mirrors media_gen's feedback).
                        _queue_media_path_notification(tool_name, media_type, path,
                                                       pool=pool, instance_name=instance_name)
                    except (MediaStorageError, ValueError) as e:
                        # Never dump the raw base64 into context (context bloat). On persist/empty
                        # failure emit a short skip marker (same pattern as the read_resource branch).
                        # Do NOT catch bare Exception here — that would mask genuine bugs (e.g. TypeError).
                        logger.warning(f'MCP media persist failed, emitting skip marker: {e}')
                        texts.append(f'[NOTE: skipped media resource: {tool_name}]')
            if texts:
                return '\n\n'.join(texts)
            else:
                return 'ERROR: execute error'

    async def cleanup(self):
        await self.exit_stack.aclose()


def _cleanup_mcp(_sig_num=None, _frame=None):
    if MCPManager._instance is None:
        return
    manager = MCPManager()
    manager.shutdown()


# Make sure all subprocesses are terminated even if killed abnormally
if threading.current_thread() is threading.main_thread():
    atexit.register(_cleanup_mcp)
