"""Grep subsystem — pattern compilation, tool availability check, and mixin for file search."""

import json
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional


# ─── Module-level cached helpers ──────────────────────────────────────────

@lru_cache(maxsize=256)
def _compile_grep_pattern(pattern: str, *, flags: int = 0):
    """Cache compiled regex patterns for grep to avoid recompiling on each call.
    
    Args:
        pattern: The regex pattern string.
        flags: Optional re.IGNORECASE flag for case-insensitive matching (smart_case).
            Keyword-only to prevent cache key collisions between positional and keyword calls.
    """
    return re.compile(pattern, flags)


@lru_cache(maxsize=1)
def _check_tool_availability():
    """Check if ripgrep or system grep are available at runtime.
    
    Returns a tuple of (rg_available, grep_available).
    Uses lru_cache for performance — tool availability doesn't change during execution.
    """
    import shutil
    import os
    rg_path = shutil.which('rg')
    grep_path = shutil.which('grep')

    rg_available = rg_path is not None
    grep_available = (grep_path is not None) and (os.name != 'nt')

    return rg_available, grep_available


# ─── Mixin: Grep methods for OperationManager ─────────────────────────────

class GrepMixin:
    """Grep/search methods. Expects self to have __init__-set attributes including class-level exclude constants."""

    # Default exclude patterns for ripgrep (glob-style) - prevents timeout on large directories
    _RG_DEFAULT_EXCLUDES = [
        '!node_modules/**',
        '!__pycache__/**',
        '!.git/**',
        '!*.pyc',
        '!*.so',
        '!*.dll',
        '!*.exe',
        '!*.zip',
        '!*.egg-info/**',
    ]

    # Default exclude patterns for standard grep (basename matching)
    _GREP_DEFAULT_EXCLUDES = [
        '*.pyc', '*.so', '*.dll', '*.exe', '*.zip',
    ]

    # Default directory excludes for GNU grep --exclude-dir (may not be available on all systems)
    _GREP_DEFAULT_EXCLUDE_DIRS = [
        'node_modules', '__pycache__', '.git', '*.egg-info',
    ]

    def _try_subprocess_grep(self, pattern: str, path: Path, include: str, char_limit: int, timeout: float,
                             exclude: str = "", ignore_vcs: bool = True, context: int = 0, smart_case: bool = True,
                             spill_file_path: Optional[str] = None):
        """Fast-path grep using system ripgrep or grep via subprocess.
        
        Returns (results_list, count, was_timed_out, was_truncated, original_output_size) on success, 
        or (None, 0, False, False, 0) on failure.
        Output format matches Python fallback: "relative_path:line_number: content"
        """
        import subprocess
        from agent_cascade.log import logger

        _rg_available, _grep_available = _check_tool_availability()

        if not _rg_available and not _grep_available:
            logger.debug("grep: subprocess fast path unavailable (rg=%s, grep=%s), falling back to Python", _rg_available, _grep_available)
            return None, 0, False, False, 0

        try:
            if _rg_available:
                cmd = [
                    'rg',
                    '-r',
                    '--no-heading',
                    '-n',
                    '--json',
                    '--color', 'never',
                    '--no-mmap',
                ]

                if not ignore_vcs:
                    cmd.extend(['--no-ignore'])

                if context > 0:
                    cmd.extend(['-C', str(context)])

                has_inline_case_flag = '(?-i:' in pattern or '(?i:' in pattern
                if smart_case:
                    if not re.search(r'[A-Z]', pattern) and not has_inline_case_flag:
                        cmd.append('-i')

                cmd.extend([
                    '--glob', include,
                ])

                for _exc in self._RG_DEFAULT_EXCLUDES:
                    cmd.extend(['--glob', _exc])

                if exclude:
                    cmd.extend(['--glob', f'!{exclude}'])

                cmd.append(pattern)
            else:
                cmd = [
                    'grep',
                    '-r',
                    '--include=' + include,
                    '-n',
                ]

                has_inline_case_flag = '(?-i:' in pattern or '(?i:' in pattern
                if smart_case:
                    if not re.search(r'[A-Z]', pattern) and not has_inline_case_flag:
                        cmd.append('-i')

                if context > 0:
                    cmd.extend(['-C', str(context)])

                if exclude:
                    cmd.append('--exclude=' + exclude)

                for _dir in self._GREP_DEFAULT_EXCLUDE_DIRS:
                    cmd.extend(['--exclude-dir', _dir])

                for _exc in self._GREP_DEFAULT_EXCLUDES:
                    cmd.append('--exclude=' + _exc)

                cmd.append(pattern)

            result = subprocess.run(
                cmd,
                cwd=str(path),
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=timeout
            )

            if result.returncode == 0:
                lines = result.stdout.split('\n') if result.stdout.strip() else []

                formatted = []

                if _rg_available:
                    match_count = 0

                    for line in lines:
                        if not line.strip():
                            continue

                        try:
                            json_obj = json.loads(line)
                            entry_type = json_obj.get('type', '')

                            if entry_type == 'match':
                                data = json_obj.get('data', {})
                                file_path = data.get('path', {}).get('text', '')

                                line_num_data = data.get('line_number', 0)
                                if isinstance(line_num_data, dict):
                                    line_num = line_num_data.get('start', 0)
                                else:
                                    line_num = line_num_data

                                match_text = data.get('lines', {}).get('text', '')

                                normalized_path = file_path.replace('\\', '/')

                                if context > 0:
                                    formatted.append(f"{normalized_path}:{line_num}: >>>{match_text}")
                                else:
                                    formatted.append(f"{normalized_path}:{line_num}: {match_text}")

                                match_count += 1

                            elif entry_type == 'context' and context > 0:
                                data = json_obj.get('data', {})
                                file_path = data.get('path', {}).get('text', '')
                                line_num_data = data.get('line_number', 0)
                                if isinstance(line_num_data, dict):
                                    line_num = line_num_data.get('start', 0)
                                else:
                                    line_num = line_num_data

                                match_text = data.get('lines', {}).get('text', '')
                                normalized_path = file_path.replace('\\', '/')
                                formatted.append(f"{normalized_path}:{line_num}:     {match_text}")

                        except json.JSONDecodeError as e:
                            logger.debug("ripgrep JSON parse error: %s", e)

                    count = match_count

                else:
                    _match_re = re.compile(r'^(.+?):(\d+):(.*)$')
                    _ctx_re = re.compile(r'^(.+?)-(\d+)-(.*)$')

                    for line in lines:
                        if not line:
                            continue
                        if line == "---" or line == "--":
                            formatted.append("---")
                            continue

                        m = _match_re.match(line)
                        if m:
                            raw_path, linenum, content = m.groups()
                            normalized_path = raw_path.replace('\\', '/')
                            if context > 0 and normalized_path.startswith(' '):
                                normalized_path = normalized_path[1:]
                                formatted.append(f"{normalized_path}:{linenum}:     {content}")
                            elif context > 0:
                                formatted.append(f"{normalized_path}:{linenum}: >>>{content}")
                            else:
                                formatted.append(f"{normalized_path}:{linenum}: {content}")
                        elif context > 0:
                            c = _ctx_re.match(line)
                            if c:
                                ctx_path, ctx_linenum, ctx_content = c.groups()
                                normalized_ctx_path = ctx_path.replace('\\', '/')
                                formatted.append(f"{normalized_ctx_path}:{ctx_linenum}:     {ctx_content}")
                            else:
                                formatted.append(line)
                        else:
                            formatted.append(line)

                    if context > 0:
                        count = sum(1 for l in formatted if ">>>" in l)
                    else:
                        count = sum(1 for l in formatted if l != "---")

                _was_truncated = False
                _original_output_size = 0
                if char_limit != -1 and count > 0:
                    output_size = sum(len(l) for l in formatted) + count
                    if output_size > char_limit:
                        _original_output_size = output_size
                        if spill_file_path is not None:
                            try:
                                full_text = '\n'.join(formatted)
                                spill_abs = self.base_dir / spill_file_path
                                spill_abs.parent.mkdir(parents=True, exist_ok=True)
                                with open(spill_abs, 'w', encoding='utf-8') as f:
                                    f.write(full_text)
                            except Exception as e:
                                logger.warning(f"Failed to write grep spill file {spill_file_path}: {e}")

                        byte_budget = char_limit
                        truncated = []
                        for line in formatted:
                            if byte_budget < len(line) + 1:
                                break
                            truncated.append(line)
                            byte_budget -= len(line) + 1
                        formatted = truncated
                        if context > 0:
                            count = sum(1 for l in formatted if ">>>" in l)
                        else:
                            count = sum(1 for l in formatted if l != "---")
                        _was_truncated = True

                return formatted, count, False, _was_truncated, _original_output_size

            # Non-zero return code (e.g., grep returns 1 for no matches) — still valid
            if result.returncode == 1:
                return [], 0, False, False, 0

        except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError, OSError) as e:
            logger.debug(f"grep subprocess unavailable (falling back to Python): {e}")

        return None, 0, False, False, 0

    def _grep_single_file(self, file_path: Path, pattern: str, char_limit: int,
                          include: str = "*", exclude: str = "", context: int = 0, smart_case: bool = True,
                          spill_file_path: Optional[str] = None, timeout: float = 30.0) -> str:
        """Search a single file for a regex pattern. Used when path is a file instead of directory."""
        import fnmatch

        try:
            normalized_rel_path = str(file_path.relative_to(self.base_dir)).replace('\\', '/')
        except ValueError:
            normalized_rel_path = file_path.name

        if not fnmatch.fnmatch(normalized_rel_path, include):
            return f"No matches found for pattern '{pattern}' in {file_path.name}"
        if exclude and fnmatch.fnmatch(normalized_rel_path, exclude):
            return f"No matches found for pattern '{pattern}' in {file_path.name} (excluded by {exclude})"

        has_inline_case_flag = '(?-i:' in pattern or '(?i:' in pattern
        if smart_case and re.search(r'[A-Z]', pattern) and not has_inline_case_flag:
            flags = 0
        elif smart_case:
            flags = re.IGNORECASE
        else:
            flags = 0

        try:
            pattern_re = _compile_grep_pattern(pattern, flags=flags)
        except re.error as e:
            return f"ERROR: Invalid regex pattern '{pattern}': {str(e)}. Please provide a valid Python regular expression."

        try:
            content = file_path.read_text(encoding='utf-8', errors='ignore')
        except Exception as e:
            return f"Error reading file {file_path.name}: {str(e)}"

        lines = content.split('\n')
        results = []
        match_count = 0
        hit_result_limit = False
        was_timed_out = False
        start_time = time.time()

        if context > 0:
            for line_num, line in enumerate(lines, 1):
                if pattern_re.search(line):
                    match_count += 1
                    start = max(1, line_num - context)
                    end = min(len(lines), line_num + context)
                    for ctx_line in range(start - 1, end):
                        prefix = ">>>" if ctx_line + 1 == line_num else "    "
                        results.append(f"{normalized_rel_path}:{ctx_line + 1}: {prefix}{lines[ctx_line]}")
                    results.append("---")
                if len(results) % 200 == 0 and time.time() - start_time > timeout:
                    was_timed_out = True
                    break
                if len(results) > 5000:
                    hit_result_limit = True
                    break
        else:
            for line_num, line in enumerate(lines, 1):
                if pattern_re.search(line):
                    match_count += 1
                    results.append(f"{normalized_rel_path}:{line_num}: {line}")
                if len(results) % 500 == 0 and time.time() - start_time > timeout:
                    was_timed_out = True
                    break
                if len(results) > 5000:
                    hit_result_limit = True
                    break

        if not results:
            return f"No matches found for pattern '{pattern}' in {file_path.name}"

        summary = f"Found {match_count} matches for '{pattern}'"
        if context > 0:
            summary += f" (with {context} line(s) of context)"
        output_text = '\n'.join(results)

        if was_timed_out:
            summary += f" [TIMED OUT after {int(timeout)}s]"
        elif hit_result_limit:
            summary += " [TRUNCATED at 5000 results]"

        if char_limit != -1 and len(output_text) > char_limit:
            full_output = output_text
            output_text = output_text[:char_limit]
            summary += " [TRUNCATED]"

            if spill_file_path is not None:
                try:
                    spill_abs = self.base_dir / spill_file_path
                    spill_abs.parent.mkdir(parents=True, exist_ok=True)
                    with open(spill_abs, 'w', encoding='utf-8') as f:
                        f.write(full_output)
                except Exception as e:
                    from agent_cascade.log import logger
                    logger.warning(f"Failed to write grep spill file {spill_file_path}: {e}")

            output_text += f"\n\n[TRUNCATED — Character limit exceeded."
            if spill_file_path is not None:
                output_text += f" Full output ({len(full_output)} chars) saved to: {spill_file_path}"
            output_text += "\nYou can read it with read_file if needed.]"

        return f"{summary}:\n\n" + output_text

    def grep(self, pattern: str, path: str = ".", include: str = "*", char_limit: int = 2000, timeout: float = 30.0, agent_name: str = "unknown",
             exclude: str = "", ignore_vcs: bool = True, context: int = 0, smart_case: bool = True,
             spill_file_path: Optional[str] = None) -> str:
        """Search for text pattern in files.
        
        Uses subprocess-based grep (ripgrep or system grep) as a fast path,
        falling back to pure Python if the subprocess approach fails/times out.
        """
        import fnmatch

        try:
            from agent_cascade.log import logger
            resolved = self._resolve_path(path)
            if not resolved.exists():
                return f"Directory not found: {path}"

            if resolved.is_file():
                return self._grep_single_file(resolved, pattern, char_limit, include=include,
                                             exclude=exclude, context=context, smart_case=smart_case,
                                             spill_file_path=spill_file_path, timeout=timeout)
            
            # ── Fast path: try subprocess-based grep (ripgrep or system grep) ──
            results, count, was_timed_out, _sub_truncated, _orig_output_size = self._try_subprocess_grep(
                pattern=pattern, path=resolved, include=include,
                char_limit=char_limit, timeout=timeout,  # Use configurable timeout
                exclude=exclude, ignore_vcs=ignore_vcs, context=context, smart_case=smart_case,
                spill_file_path=spill_file_path
            )
            if results is not None:
                if count == 0 and not _sub_truncated:
                    logger.debug(f"grep: subprocess found no matches for '{pattern}', trying Python fallback")
                else:
                    output_text = '\n'.join(results)
                    if _sub_truncated and count == 0:
                        summary = f"Matches found for '{pattern}' [TRUNCATED]"
                    else:
                        summary = f"Found {count} matches for '{pattern}'"
                    if context > 0:
                        summary += f" (with {context} line(s) of context)"
                    if was_timed_out:
                        summary += f" [TIMED OUT after {int(timeout)}s]"

                    if char_limit != -1 and len(output_text) > char_limit:
                        full_output = output_text
                        output_text = output_text[:char_limit]

                        if spill_file_path is not None:
                            try:
                                spill_abs = self.base_dir / spill_file_path
                                spill_abs.parent.mkdir(parents=True, exist_ok=True)
                                with open(spill_abs, 'w', encoding='utf-8') as f:
                                    f.write(full_output)
                            except Exception as e:
                                logger.warning(f"Failed to write grep spill file {spill_file_path}: {e}")

                        output_text += f"\n\n[TRUNCATED — Character limit exceeded."
                        if spill_file_path is not None:
                            output_text += f" Full output ({len(full_output)} chars) saved to: {spill_file_path}"
                        output_text += "\nYou can read it with read_file if needed.]"
                    elif _sub_truncated and spill_file_path is not None:
                        summary += " [TRUNCATED]"
                        output_text += f"\n\n[TRUNCATED — Character limit exceeded. Full output ({_orig_output_size} chars) saved to: {spill_file_path}\nYou can read it with read_file if needed.]"

                    return f"{summary}:\n\n" + output_text

            # ── Slow path: pure Python fallback ──
            _rg_avail, _grep_avail = _check_tool_availability()
            logger.debug(f"grep: subprocess fast path unavailable (rg={_rg_avail}, grep={_grep_avail}), falling back to Python")
            results = []

            has_inline_case_flag = '(?-i:' in pattern or '(?i:' in pattern
            if smart_case and re.search(r'[A-Z]', pattern) and not has_inline_case_flag:
                flags = 0
            elif smart_case:
                flags = re.IGNORECASE
            else:
                flags = 0
            try:
                pattern_re = _compile_grep_pattern(pattern, flags=flags)
            except re.error as e:
                return f"ERROR: Invalid regex pattern '{pattern}': {str(e)}. Please provide a valid Python regular expression."

            start_time = time.time()
            # Use the configurable timeout (already passed as parameter)
            was_timed_out = False
            hit_result_limit = False
            file_count = 0
            match_count = 0

            skip_dirs = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'dist', 'build', '.tox'}

            for file_path in resolved.rglob(include):
                if time.time() - start_time > timeout:
                    was_timed_out = True
                    break
                if file_path.is_file():
                    if ignore_vcs:
                        parts = file_path.relative_to(resolved).parts
                        if any(p in skip_dirs for p in parts):
                            continue
                    if exclude:
                        try:
                            rel = file_path.relative_to(resolved)
                            if fnmatch.fnmatch(str(rel), exclude):
                                continue
                        except ValueError as e:
                            logger.debug(f"Relative path resolution failed for {file_path} (using fallback): {e}")
                    try:
                        content = file_path.read_text(encoding='utf-8', errors='ignore')
                        lines = content.split('\n')

                        if context > 0:
                            try:
                                normalized_rel_path = str(file_path.relative_to(resolved)).replace('\\', '/')
                            except ValueError:
                                try:
                                    normalized_rel_path = str(file_path.relative_to(self.base_dir)).replace('\\', '/')
                                except ValueError:
                                    normalized_rel_path = file_path.name

                            for line_num, line in enumerate(lines, 1):
                                if pattern_re.search(line):
                                    match_count += 1
                                    start = max(1, line_num - context)
                                    end = min(len(lines), line_num + context)
                                    for ctx_line in range(start - 1, end):
                                        prefix = ">>>" if ctx_line + 1 == line_num else "    "
                                        results.append(f"{normalized_rel_path}:{ctx_line + 1}: {prefix}{lines[ctx_line]}")
                                    results.append("---")
                                if len(results) % 200 == 0 and time.time() - start_time > timeout:
                                    was_timed_out = True
                                    break
                                if len(results) > 5000:
                                    hit_result_limit = True
                                    break
                        else:
                            try:
                                rel_path = file_path.relative_to(resolved)
                            except ValueError:
                                try:
                                    rel_path = file_path.relative_to(self.base_dir)
                                except ValueError:
                                    rel_path = file_path.name
                            normalized_rel_path = str(rel_path).replace('\\', '/')

                            for line_num, line in enumerate(lines, 1):
                                if pattern_re.search(line):
                                    match_count += 1
                                    results.append(f"{normalized_rel_path}:{line_num}: {line}")
                                if len(results) % 500 == 0 and time.time() - start_time > timeout:
                                    was_timed_out = True
                                    break
                            if was_timed_out:
                                break

                        file_count += 1
                        if len(results) > 5000:
                            hit_result_limit = True
                            break
                    except Exception as e:
                        logger.debug(f"Error reading file during grep (skipping): {e}")
                        continue

            if not results and not was_timed_out:
                logger.debug(f"grep: Python fallback also found no matches for '{pattern}' (subprocess already confirmed)")

            if not results:
                if was_timed_out:
                    return f"Search timed out after {int(timeout)}s before finding any matches for '{pattern}'. Narrow your pattern or scope."
                exclude_info = f", excluding {exclude}" if exclude else ""
                return f"No matches found for pattern '{pattern}' in {path}/**/{include}{exclude_info}"

            summary = f"Found {match_count} matches for '{pattern}'"
            if context > 0:
                summary += f" (with {context} line(s) of context)"
            output_text = '\n'.join(results)

            if was_timed_out:
                summary += f" [TIMED OUT after {int(timeout)}s]"
                output_text += f"\n\n[TOOL RESPONSE TIMED OUT — Searched {file_count} files before exceeding {int(timeout)} second limit. Narrow your pattern or scope to a specific directory.]"
            elif hit_result_limit:
                summary += " [TRUNCATED at 5000 results]"

            if char_limit != -1 and len(output_text) > char_limit:
                full_output = output_text
                output_text = output_text[:char_limit]
                summary += " [TRUNCATED]"

                if spill_file_path is not None:
                    try:
                        spill_abs = self.base_dir / spill_file_path
                        spill_abs.parent.mkdir(parents=True, exist_ok=True)
                        with open(spill_abs, 'w', encoding='utf-8') as f:
                            f.write(full_output)
                    except Exception as e:
                        logger.warning(f"Failed to write grep spill file {spill_file_path}: {e}")

                output_text += f"\n\n[TRUNCATED — Character limit exceeded."
                if spill_file_path is not None:
                    output_text += f" Full output ({len(full_output)} chars) saved to: {spill_file_path}"
                output_text += "\nYou can read it with read_file if needed.]"

            return f"{summary}:\n\n" + output_text
        except Exception as e:
            return f"Error searching: {str(e)}"