"""File operations — directory listing, read, write, edit, re-indent, delete, copy, move, backup cleanup."""

import fnmatch
import re
import shutil
import time
import zipfile
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ─── Mixin: File operations for OperationManager ─────────────────────────

class FileOpsMixin:
    """File operation methods. Expects self to have __init__-set attributes."""

    # ─── Backup cleanup (registered via atexit in __init__) ──────────────

    def cleanup_backups(self, agent_name: Optional[str] = None):
        """Archive .bak backup files into zip archives and remove the originals.

        If agent_name is provided, only processes that agent's backup directory.
        Otherwise, performs global cleanup across all agents.
        """
        try:
            from agent_cascade.log import logger
            backup_base = self.base_dir / 'logs' / 'backups'
            if not backup_base.exists():
                logger.debug("Backup directory does not exist: %s", backup_base)
                return

            if agent_name:
                safe_agent = re.sub(r'[^a-zA-Z0-9_-]', '_', agent_name)
                agent_backup_dir = backup_base / safe_agent
                if not agent_backup_dir.exists():
                    logger.debug("Agent backup directory does not exist: %s", agent_backup_dir)
                    return

                archive_path = agent_backup_dir / 'backup_archive.zip'
                bak_files = list(agent_backup_dir.glob('*.bak'))

                if not bak_files:
                    logger.debug("No .bak files to archive for agent %s", agent_name)
                else:
                    if archive_path.exists():
                        timestamp = int(time.time())
                        archive_path.rename(archive_path.with_name(f'backup_archive.{timestamp}.zip'))

                    old_zips = list(agent_backup_dir.glob('backup_archive.*.zip'))

                    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                        for bak_file in bak_files:
                            zf.write(bak_file, arcname=bak_file.name)
                        for old_zip in old_zips:
                            zf.write(old_zip, arcname=old_zip.name)

                    logger.debug("Archived %d .bak files to %s", len(bak_files), archive_path)

                for old_zip in agent_backup_dir.glob('backup_archive.*.zip'):
                    try:
                        old_zip.unlink()
                    except Exception as e:
                        logger.warning("Failed to delete old archive %s: %s", old_zip, e)

                if bak_files:
                    for bak_file in bak_files:
                        try:
                            bak_file.unlink()
                        except Exception as e:
                            logger.warning("Failed to delete backup file %s: %s", bak_file, e)

            else:
                archive_path = backup_base / 'backup_archive.zip'
                bak_files = list(backup_base.rglob('*.bak'))

                if not bak_files:
                    logger.debug("No .bak files to archive globally")
                else:
                    if archive_path.exists():
                        timestamp = int(time.time())
                        archive_path.rename(archive_path.with_name(f'backup_archive.{timestamp}.zip'))

                    old_zips = list(backup_base.glob('backup_archive.*.zip'))

                    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                        for bak_file in bak_files:
                            arcname = str(bak_file.relative_to(backup_base))
                            zf.write(bak_file, arcname=arcname)
                        for old_zip in old_zips:
                            zf.write(old_zip, arcname=old_zip.name)

                    logger.debug("Archived %d .bak files globally to %s", len(bak_files), archive_path)

                for old_zip in backup_base.glob('backup_archive.*.zip'):
                    try:
                        old_zip.unlink()
                    except Exception as e:
                        logger.warning("Failed to delete old archive %s: %s", old_zip, e)

                if bak_files:
                    for bak_file in bak_files:
                        try:
                            bak_file.unlink()
                        except Exception as e:
                            logger.warning("Failed to delete backup file %s: %s", bak_file, e)

        except Exception as e:
            from agent_cascade.log import logger
            logger.warning("Failed to clean up backups: %s", e)

    # ─── Static helpers for formatting and filtering ─────────────────────

    @staticmethod
    def _format_size(size_bytes: Optional[int]) -> str:
        """Convert bytes to human-readable string (B, KB, MB, GB)."""
        if size_bytes is None:
            return "?"
        if size_bytes < 1024:
            return f"{size_bytes} B"
        units = ['KB', 'MB', 'GB', 'TB']
        idx = -1
        val = float(size_bytes)
        while val >= 1024 and idx < len(units) - 1:
            val /= 1024
            idx += 1
        return f"{val:.1f} {units[idx]}"

    @staticmethod
    def _format_mtime(mtime_value) -> str:
        """Format modification time from a float timestamp like '2026-06-27 14:32'."""
        if mtime_value is None:
            return "?"
        try:
            dt = datetime.fromtimestamp(mtime_value)
            return dt.strftime('%Y-%m-%d %H:%M')
        except (OSError, ValueError):
            return "?"

    @staticmethod
    def _matches_filters(name: str, include_fn, exclude_fn) -> bool:
        """Check if a name passes both include and exclude filters."""
        if include_fn and not include_fn(name):
            return False
        if exclude_fn and not exclude_fn(name):
            return False
        return True

    @staticmethod
    def _make_entry(name: str, is_dir: bool, stat_info) -> dict:
        """Create a standardized entry dict from a name, type flag, and optional stat info."""
        if stat_info is None:
            return {'name': name, 'is_dir': is_dir, 'size': 0 if is_dir else None, 'mtime': None}
        return {
            'name': name,
            'is_dir': is_dir,
            'size': 0 if is_dir else stat_info.st_size,
            'mtime': stat_info.st_mtime,
        }

    @staticmethod
    def _sort_entries(entries: list, sort_by: str) -> list:
        """Sort entries: directories always before files, then by requested key."""
        dirs = [e for e in entries if e['is_dir']]
        files = [e for e in entries if not e['is_dir']]

        def sort_key(entry):
            name_lower = entry['name'].lower()
            size = entry.get('size') or 0
            mtime = entry.get('mtime') or 0
            ext = Path(entry['name']).suffix.lower()
            return {
                'name': (name_lower,),
                'size': (-size, name_lower),
                'date': (-mtime, name_lower),
                'type': (ext, name_lower),
            }.get(sort_by, (name_lower,))

        dirs.sort(key=sort_key)
        files.sort(key=sort_key)

        return dirs + files

    # ─── Directory listing helpers ────────────────────────────────────────

    def _list_flat(
        self, resolved: Path, include_fn, exclude_fn, sort_by: str, max_entries: int
    ) -> Tuple[str, int, int, int]:
        """Flat directory listing via os.scandir. Returns (output_str, dirs, files, size)."""
        import os
        dirs = []
        files = []

        with os.scandir(str(resolved)) as it:
            for entry in it:
                if not self._matches_filters(entry.name, include_fn, exclude_fn):
                    continue
                try:
                    stat_info = entry.stat()
                except Exception:
                    stat_info = None
                is_dir = entry.is_dir()
                (dirs if is_dir else files).append(
                    self._make_entry(entry.name, is_dir, stat_info)
                )

        sorted_dirs = self._sort_entries(dirs, sort_by)
        sorted_files = self._sort_entries(files, sort_by)

        overflow_count = 0
        if len(sorted_dirs) + len(sorted_files) > max_entries:
            remaining = max_entries
            show_dirs = sorted_dirs[:remaining]
            remaining -= len(show_dirs)
            show_files = sorted_files[:remaining]
            overflow_count = (len(sorted_dirs) - len(show_dirs)) + (len(sorted_files) - len(show_files))
            sorted_dirs, sorted_files = show_dirs, show_files

        total_dirs = len(sorted_dirs)
        total_files = len(sorted_files)
        total_size = sum(f['size'] for f in sorted_files if f.get('size'))

        output = ""
        if sorted_dirs:
            output += "Directories:\n"
            for e in sorted_dirs:
                output += f"  {e['name']}/ (modified: {self._format_mtime(e['mtime'])})\n"
        if sorted_files:
            output += "\nFiles:\n"
            for e in sorted_files:
                output += f"  {e['name']} ({self._format_size(e['size'])}, modified: {self._format_mtime(e['mtime'])})\n"
        if overflow_count > 0:
            output += f"  ... and {overflow_count} more entries (output limited to {max_entries}); use include/exclude to narrow\n"

        return output, total_dirs, total_files, total_size

    def _list_recursive(
        self, path: str, resolved: Path, include_fn, exclude_fn, sort_by: str, max_depth: int, max_entries: int
    ) -> Tuple[str, int, int, int]:
        """Recursive directory listing via os.walk. Returns (output_str, dirs, files, size)."""
        entries_by_dir: dict = OrderedDict()
        visited_dirs: set = set()
        entry_count = 0
        root_depth = len(resolved.parts)

        for dirpath, subdirs, filenames in os.walk(str(resolved), topdown=True):
            current_dir = Path(dirpath)
            abs_path = current_dir.resolve()

            if abs_path in visited_dirs:   # symlink cycle guard
                subdirs.clear()
                continue
            visited_dirs.add(abs_path)

            current_depth = len(abs_path.parts) - root_depth
            dir_entries = []

            for d_name in sorted(subdirs):
                if entry_count >= max_entries:
                    break
                try:
                    stat_info = (current_dir / d_name).stat()
                except Exception:
                    stat_info = None
                if not self._matches_filters(d_name, include_fn, exclude_fn):
                    continue
                dir_entries.append(self._make_entry(d_name, True, stat_info))
                entry_count += 1

            for f_name in sorted(filenames):
                if entry_count >= max_entries:
                    break
                try:
                    stat_info = (current_dir / f_name).stat()
                except Exception:
                    stat_info = None
                if not self._matches_filters(f_name, include_fn, exclude_fn):
                    continue
                dir_entries.append(self._make_entry(f_name, False, stat_info))
                entry_count += 1

            if dir_entries:
                entries_by_dir[dirpath] = self._sort_entries(dir_entries, sort_by)

            if entry_count >= max_entries:
                subdirs.clear()
                continue
            if max_depth != -1 and current_depth >= max_depth - 1:
                subdirs.clear()

        total_dirs = sum(1 for g in entries_by_dir.values() for e in g if e['is_dir'])
        total_files = sum(1 for g in entries_by_dir.values() for e in g if not e['is_dir'])
        total_size = sum(e['size'] or 0 for g in entries_by_dir.values() for e in g if not e['is_dir'])

        output = ""
        for dir_path, group in entries_by_dir.items():
            rel_label = str(Path(dir_path).relative_to(resolved)) if Path(dir_path) != resolved else path + "/"
            output += f"\n[{rel_label}]\n" if rel_label != path + "/" else f"[{rel_label}]\n"
            for e in group:
                if e['is_dir']:
                    output += f"[DIR] {e['name']}/ (modified: {self._format_mtime(e['mtime'])})\n"
                else:
                    output += f"  {e['name']} ({self._format_size(e['size'])}, modified: {self._format_mtime(e['mtime'])})\n"

        if entry_count >= max_entries and entries_by_dir:
            output += f"\n  ... (listing stopped at {max_entries} entries; use include/exclude to narrow)\n"

        return output, total_dirs, total_files, total_size

    # ─── Public directory listing API ─────────────────────────────────────

    def list_directory(
        self,
        path: str = ".",
        recursive: bool = False,
        max_depth: int = -1,
        include: Optional[str] = None,
        exclude: Optional[str] = None,
        sort_by: str = "name",
        show_summary: bool = False,
        max_entries: int = 500,
    ) -> str:
        """List contents of a directory with optional recursive traversal, filtering, sorting, and summary."""
        try:
            resolved = self._resolve_path(path)
            if not resolved.exists():
                return f"Directory not found: {path}"
            if not resolved.is_dir():
                return f"Not a directory: {path}"

            if max_depth < 0:
                max_depth = -1
            if recursive and max_depth == 0:
                recursive = False

            valid_sort_keys = {"name", "size", "date", "type"}
            if sort_by not in valid_sort_keys:
                sort_by = "name"

            include_fn = (lambda name: fnmatch.fnmatch(name, include)) if include else None
            exclude_fn = (lambda name: fnmatch.fnmatch(name, exclude)) if exclude else None

            header = f"Contents of {path}/ (Absolute path: {resolved}):\n\n"

            if recursive:
                output, total_dirs, total_files, total_size = self._list_recursive(
                    path, resolved, include_fn, exclude_fn, sort_by, max_depth, max_entries
                )
                is_empty = not (total_dirs or total_files)
            else:
                output, total_dirs, total_files, total_size = self._list_flat(
                    resolved, include_fn, exclude_fn, sort_by, max_entries
                )
                is_empty = not (total_dirs or total_files)

            if is_empty:
                output += "  (empty directory or all entries filtered out)"

            if show_summary:
                output += f"\nSummary:\n"
                output += f"  Total directories: {total_dirs}\n"
                output += f"  Total files:       {total_files}\n"
                output += f"  Total size:        {self._format_size(total_size)}\n"

            return header + output
        except Exception as e:
            from agent_cascade.log import logger
            logger.debug(f"Error listing directory: {e}")
            return f"Error listing directory: {str(e)}"

    # ─── Read file ────────────────────────────────────────────────────────

    def read_file(self, path: str, start_line: int = 1, limit: int = 1000) -> str:
        """Read a file. Uses line-by-line iteration for memory efficiency when range is specified."""
        try:
            resolved = self._resolve_path(path, mode="ro")
            if not resolved.exists():
                return f"File not found: {path}"
            if not resolved.is_file():
                return f"Not a file: {path}"

            end_line = start_line + limit - 1
            total_lines = 0
            hit_end = False

            with open(resolved, 'r', encoding='utf-8', errors='ignore') as f:
                lines = []
                for line_num, line in enumerate(f, 1):
                    total_lines = line_num
                    if line_num < start_line:
                        continue
                    if line_num > end_line:
                        hit_end = True
                        break
                    lines.append(line.rstrip('\n'))

            if hit_end:
                total_lines_str = f">{total_lines}"
            else:
                total_lines_str = str(total_lines)
            content = "".join([f"{start_line + i}: {lines[i]}" for i in range(len(lines))])
            header = f"File content ({path}), lines {start_line} to {start_line + len(lines) - 1} of {total_lines_str}:"
            if hit_end:
                header += " [TRUNCATED]"

            return f"{header}\n```\n{content}\n```"
        except Exception as e:
            return f"Error reading file: {str(e)}"

    # ─── Write file ──────────────────────────────────────────────────────

    def write_file(self, path: str, content: str, agent_name: str) -> str:
        """Write a file — auto-approved for new files and owned files."""
        try:
            resolved = self._resolve_path(path, mode="rw")
        except Exception as e:
            return f"ERROR: {str(e)}"
        is_new = not resolved.exists()

        if not self._is_auto_approved(path, agent_name, creating_new=True):
            description = f"Overwrite existing file: {path} ({len(content)} chars)"
            approved, reason = self.request_user_approval(
                agent_name=agent_name,
                tool_name='write_file',
                tool_args={'path': path, 'content': content},
                description=description,
            )
            if not approved:
                return f"REJECTED BY USER: {reason}"
            justification = reason
        else:
            justification = ""

        try:
            resolved = self._resolve_path(path, mode="rw")

            # Backup if overwriting
            backup_path_str = ""
            if resolved.exists():
                safe_agent = re.sub(r'[^a-zA-Z0-9_-]', '_', agent_name)
                backup_dir = self.base_dir / "logs" / "backups" / safe_agent
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup_path = backup_dir / f"{resolved.name}.{int(time.time())}.bak"
                shutil.copy2(resolved, backup_path)
                try:
                    backup_path_str = str(backup_path.relative_to(self.base_dir))
                except ValueError:
                    backup_path_str = str(backup_path)

            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding='utf-8')
            self.file_ownership[str(resolved)] = agent_name
            msg = f"APPROVED: Created {path} ({len(content)} characters)"
            if justification:
                msg += f"\nSecurity Justification: {justification}"
            if backup_path_str:
                msg += f". Backup created: {backup_path_str}"
            return msg
        except Exception as e:
            return f"ERROR: Approved but execution failed: {str(e)}"

    # ─── Edit file (with nested helpers) ──────────────────────────────────

    def edit_file(self, path: str, agent_name: str,
                  old_content: str,
                  new_content: str,
                  match_mode: str = 'exact') -> str:
        """Edit a file surgically — auto-approved for agent-owned files."""
        try:
            resolved = self._resolve_path(path, mode="rw")
        except Exception as e:
            return f"ERROR: {str(e)}"

        if not resolved.exists():
            return f"File not found for surgical edit: {path}"

        file_content = resolved.read_text(encoding='utf-8')
        actual_old_content = old_content
        match_ratio = 1.0

        from agent_cascade.settings import DEFAULT_HEURISTIC_MATCH_THRESHOLD
        import difflib

        # ── Helper: parse range spec for delete_and_insert mode ──────────────
        def _parse_range(range_str: str, total_lines: int) -> tuple:
            """Parse old_content as a line range for delete_and_insert mode.
            
            Returns (start_idx, end_idx) as 0-based Python slice indices.
            - start is inclusive, end is exclusive.
            - 1-indexed input: '3:7' means lines 3 through 7.
            - Single number '4' means insert before line 4 (empty delete range).
            - Negative numbers count from end: -1 = last line.
            - 0 means append at end of file.
            """
            range_str = range_str.strip()
            
            if ':' in range_str:
                parts = range_str.split(':')
                if len(parts) != 2:
                    raise ValueError(f"Range must have exactly one ':'. Got '{range_str}'")
                
                start_part, end_part = parts
                
                # Parse start (empty means delete all from beginning, i.e., start at line 1)
                if start_part.strip() == '':
                    start = 1  # Delete from the very first line
                elif start_part.strip() == '0':
                    start = total_lines + 1  # Append beyond last line
                else:
                    start = int(start_part)
                
                # Parse end (empty means delete all from start to end of file)
                if end_part.strip() == '':
                    end = total_lines + 1  # Delete everything from start onward
                elif end_part.strip() == '0':
                    end = total_lines + 1  # Append at end (same as empty)
                else:
                    end = int(end_part)
                if start < 0:
                    start = total_lines + 1 + start  # 1-indexed: -1 → last line
                if end < 0:
                    end = total_lines + end  # exclusive end: -1 → stop before last
                
                # Clamp to valid bounds
                start = max(0, min(start, total_lines + 1))
                end = max(0, min(end, total_lines + 1))
                
                if start > end:
                    raise ValueError(f"Start ({start}) must be <= end ({end})")
                
                # Convert to 0-based slice indices
                return start - 1, end
                
            else:
                # Single number = insert-only (or delete single line if new_content is empty)
                if range_str == '0':
                    return total_lines, total_lines  # Append at end
                start = int(range_str)
                if start < 0:
                    start = total_lines + 1 + start
                # Clamp to [1, total_lines+1] so that out-of-range means append
                start = max(1, min(start, total_lines + 1))
                zero_idx = start - 1  # Convert to 0-based index
                return zero_idx, zero_idx  # Empty range at position (insert point)

        def _detect_line_ending(line: str) -> str:
            """Detect the line ending style of a line."""
            if '\r\n' in line:
                return '\r\n'
            elif '\n' in line:
                return '\n'
            elif '\r' in line:
                return '\r'
            return ''

        if match_mode == 'exact':
            count = file_content.count(old_content)
            if count == 0:
                return f"ERROR: Pattern not found in {path}. The 'old_content' string must exactly match the existing file content character-for-character, including whitespace and indentation, or consider using heuristic match mode."
            if count > 1:
                return f"ERROR: Pattern found {count} times in {path}. The 'old_content' block must be unique. Please include more surrounding lines in 'old_content' to make it unique."
        elif match_mode in ('heuristic', 'heuristic_agnostic'):
            file_lines = file_content.splitlines(keepends=True)

            # Map normalized (whitespace-stripped, non-blank) lines of the raw file
            file_line_info = []
            for idx, line in enumerate(file_lines):
                norm = "".join(line.split())
                if norm:
                    file_line_info.append((idx, norm))

            old_line_info = []
            for line in old_content.splitlines(keepends=True):
                norm = "".join(line.split())
                if norm:
                    old_line_info.append(norm)

            if not old_line_info:
                return "ERROR: The 'old_content' contains only whitespace. Heuristic match mode requires at least some non-whitespace content to match."

            file_line_map = {}
            for list_idx, (orig_idx, norm) in enumerate(file_line_info):
                if norm not in file_line_map:
                    file_line_map[norm] = []
                file_line_map[norm].append(list_idx)

            candidates = set()
            n_old_non_empty = len(old_line_info)
            n_file_non_empty = len(file_line_info)

            for old_idx, norm in enumerate(old_line_info):
                if norm and norm in file_line_map:
                    if len(file_line_map[norm]) <= 20:
                        for list_idx in file_line_map[norm]:
                            start_list_idx = list_idx - old_idx
                            if 0 <= start_list_idx <= n_file_non_empty - n_old_non_empty:
                                candidates.add(start_list_idx)

            if len(candidates) > 100:
                return f"ERROR: Heuristic pattern is too ambiguous (found {len(candidates)} candidate locations). Please include more unique surrounding lines of context."

            norm_old_joined = "".join(old_line_info)
            threshold = DEFAULT_HEURISTIC_MATCH_THRESHOLD
            matches = []

            for start_list_idx in candidates:
                best_ratio = 0.0
                best_match_info = None

                for size in range(max(1, n_old_non_empty - 2), min(n_file_non_empty - start_list_idx + 1, n_old_non_empty + 3)):
                    candidate_slice = file_line_info[start_list_idx : start_list_idx + size]
                    candidate_norms = [item[1] for item in candidate_slice]
                    norm_candidate_joined = "".join(candidate_norms)

                    ratio = difflib.SequenceMatcher(None, norm_old_joined, norm_candidate_joined).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_match_info = {
                            'start_list_idx': start_list_idx,
                            'end_list_idx': start_list_idx + size,
                            'ratio': ratio
                        }

                if best_match_info and best_ratio >= threshold:
                    matches.append(best_match_info)

            if len(matches) == 0:
                return f"ERROR: Heuristic pattern not found in {path} (threshold={threshold:.0%})."
            if len(matches) > 1:
                return f"ERROR: Heuristic pattern found {len(matches)} times in {path} above the similarity threshold. The pattern must be unique."

            unique_match = matches[0]
            orig_start_idx = file_line_info[unique_match['start_list_idx']][0]
            orig_end_idx = file_line_info[unique_match['end_list_idx'] - 1][0]

            actual_old_content = "".join(file_lines[orig_start_idx : orig_end_idx + 1])
            match_ratio = unique_match['ratio']

            last_matched_line = file_lines[orig_end_idx]

            # ── Helper functions for indentation preservation ──────────────
            def get_leading_whitespace(s: str) -> str:
                """Get leading whitespace of first non-blank line."""
                for line in s.splitlines():
                    if line.strip():
                        return line[:len(line) - len(line.lstrip())]
                return ""

            def get_indent_width(indent_str: str) -> int:
                """Calculate indent width in spaces (tab=4)."""
                return sum(4 if c == '\t' else 1 for c in indent_str if c in ' \t')

            def detect_indent_char(indent_str: str) -> str:
                """Detect whether file uses tabs or spaces for indentation."""
                return '\t' if '\t' in indent_str else ' '

            file_indent = get_leading_whitespace(actual_old_content)
            old_indent = get_leading_whitespace(old_content)
            delta_width = get_indent_width(file_indent) - get_indent_width(old_indent)

            # Detect file type to choose appropriate normalization mode
            _PYTHON_EXTENSIONS = frozenset(('.py', '.pyi', '.pyx'))
            _is_python_file = resolved.suffix.lower() in _PYTHON_EXTENSIONS

            def normalize_line_generic(line: str) -> str:
                return line.strip()

            def _normalize_line_python(line: str) -> str:
                result = line
                result = re.sub(r'"[^"]*"', '', result)
                result = re.sub(r"'[^']*'", '', result)
                result = re.sub(r'\[(\d+\.\d+)\]', '[]', result)
                result = re.sub(r'\b\d+\.\d+\b', '', result)
                result = re.sub(r'\b\d+\.?\d*[eE][+-]?\d+\b', '', result)
                result = re.sub(r'(?<!\[)\b\d+\b(?!])', '', result)
                result = re.sub(r'\b0[xX][0-9a-fA-F]+\b', '', result)
                result = re.sub(r'\b0[bB][01]+\b', '', result)
                result = re.sub(r'\b0[oO][0-7]+\b', '', result)
                result = re.sub(r'\b[a-zA-Z_][a-zA-Z0-9_.\[\]()]*\s*(<<=|>>=|\*=|/=|//=|%=|\+=|-=|\|=|&=|\^=)', 'assign', result)
                result = re.sub(r'\b[a-zA-Z_][a-zA-Z0-9_.\[\]()]*\s*=(?!=)', 'assign', result)
                result = re.sub(r'\breturn\b.*', 'return', result)
                result = re.sub(r'\b(?:True|False)\b', '', result)
                prev = None
                while prev != result:
                    prev = result
                    result = re.sub(r'assign\s*[a-zA-Z_]\w*', 'assign', result)
                return "".join(result.split())

            def normalize_line_for_alignment(line: str) -> str:
                if _is_python_file and match_mode == 'heuristic':
                    return _normalize_line_python(line)
                else:
                    return normalize_line_generic(line)

            old_norm_lines = [normalize_line_for_alignment(l) for l in old_content.splitlines()]
            file_norm_lines = [normalize_line_for_alignment(l) for l in actual_old_content.splitlines()]
            new_norm_lines = [normalize_line_for_alignment(l) for l in new_content.splitlines()]

            # Build alignment: old_content line index -> file block line index
            matcher = difflib.SequenceMatcher(None, old_norm_lines, file_norm_lines)
            old_to_file_map = {}
            for tag, i1_start, i1_end, j1_start, j1_end in matcher.get_opcodes():
                if tag == 'equal':
                    for a, b in zip(range(i1_start, i1_end), range(j1_start, j1_end)):
                        old_to_file_map[a] = b
                elif tag == 'replace':
                    sub_matcher = difflib.SequenceMatcher(
                        None,
                        old_norm_lines[i1_start:i1_end],
                        file_norm_lines[j1_start:j1_end]
                    )
                    for tag, a_s, a_e, b_s, b_e in sub_matcher.get_opcodes():
                        if tag == 'equal':
                            for a, b in zip(range(a_s, a_e), range(b_s, b_e)):
                                old_to_file_map[i1_start + a] = j1_start + b

            # Build alignment: new_content line index -> old_content line index
            new_to_old_map = {}
            matcher2 = difflib.SequenceMatcher(None, new_norm_lines, old_norm_lines)
            for tag, i1_start, i1_end, j1_start, j1_end in matcher2.get_opcodes():
                if tag == 'equal':
                    for a, b in zip(range(i1_start, i1_end), range(j1_start, j1_end)):
                        new_to_old_map[a] = b

            # Combine: new_content line -> file block line (via old_content as bridge)
            new_to_file_map = {}
            for new_idx, old_idx in new_to_old_map.items():
                if old_idx in old_to_file_map:
                    new_to_file_map[new_idx] = old_to_file_map[old_idx]

            # Record original indents from the file block
            file_block_lines = actual_old_content.splitlines(keepends=True)
            file_indent_by_line = {}
            for idx, fl in enumerate(file_block_lines):
                norm = "".join(fl.split())
                if norm:
                    leading_ws = fl[:len(fl) - len(fl.lstrip())] if fl.strip() else ""
                    file_indent_by_line[idx] = leading_ws

            def find_best_indent_for_unmapped_line(
                    line_idx: int,
                    new_content_lines: list,
                    new_to_file_map: dict,
                    file_indent_by_line: dict) -> str:
                for check_idx in range(line_idx - 1, -1, -1):
                    if check_idx in new_to_file_map:
                        f_idx = new_to_file_map[check_idx]
                        if f_idx in file_indent_by_line:
                            return file_indent_by_line[f_idx]
                for check_idx in range(line_idx + 1, len(new_content_lines)):
                    if check_idx in new_to_file_map:
                        f_idx = new_to_file_map[check_idx]
                        if f_idx in file_indent_by_line:
                            return file_indent_by_line[f_idx]
                return file_indent if file_indent else ""

            # Phase 2 — Preservation: apply file indents to new_content lines
            new_content_lines = new_content.splitlines(keepends=True)
            adjusted_lines = []

            for line_idx, line in enumerate(new_content_lines):
                if not line.strip():
                    if file_indent != old_indent and delta_width != 0:
                        indent_char = detect_indent_char(file_indent)
                        if indent_char == '\t':
                            base_tabs = max(0, round(get_indent_width(file_indent) / 4))
                            adjusted_lines.append(('\t' * base_tabs) + line.lstrip(' \t'))
                        else:
                            adjusted_lines.append((' ' * max(0, get_indent_width(file_indent))) + line.lstrip(' \t'))
                    else:
                        adjusted_lines.append(line)
                    continue

                if line_idx in new_to_file_map:
                    f_idx = new_to_file_map[line_idx]
                    if f_idx in file_indent_by_line:
                        orig_leading_ws = file_indent_by_line[f_idx]
                        adjusted_lines.append(orig_leading_ws + line.lstrip())
                        continue

                best_indent = find_best_indent_for_unmapped_line(
                    line_idx, new_content_lines, new_to_file_map, file_indent_by_line)
                if best_indent:
                    adjusted_lines.append(best_indent + line.lstrip())
                elif file_indent != old_indent and delta_width != 0:
                    current_indent = line[:len(line) - len(line.lstrip())]
                    current_width = get_indent_width(current_indent)

                    indent_char = detect_indent_char(file_indent)
                    if indent_char == '\t':
                        delta_tabs = round(delta_width / 4)
                        new_tabs = max(0, (current_width // 4) + delta_tabs)
                        adjusted_lines.append(('\t' * new_tabs) + line.lstrip())
                    else:
                        new_spaces = max(0, current_width + delta_width)
                        adjusted_lines.append((' ' * new_spaces) + line.lstrip())
                elif file_indent:
                    adjusted_lines.append(file_indent + line.lstrip())
                else:
                    adjusted_lines.append(line)

            new_content = "".join(adjusted_lines)

            # Phase 3 — Validation: increment-based indentation anomaly detection
            from collections import Counter

            def validate_indentation_consistency(content: str, file_path: str) -> list:
                warnings = []
                indent_widths = []

                for i, line in enumerate(content.splitlines()):
                    if not line.strip():
                        continue
                    leading_ws = line[:len(line) - len(line.lstrip())]
                    width = get_indent_width(leading_ws)
                    indent_widths.append((i + 1, width))

                if len(indent_widths) >= 2:
                    increments = [abs(indent_widths[j][1] - indent_widths[j-1][1])
                                  for j in range(1, len(indent_widths))]
                    positive_increments = [inc for inc in increments if inc > 0]

                    if len(positive_increments) >= 2:
                        typical_increment = Counter(positive_increments).most_common(1)[0][0]
                        threshold_val = max(typical_increment * 3, 8)
                    else:
                        threshold_val = 16

                    for j in range(1, len(indent_widths)):
                        prev_line_num, prev_w = indent_widths[j - 1]
                        curr_line_num, curr_w = indent_widths[j]
                        diff = abs(curr_w - prev_w)
                        if diff > threshold_val:
                            direction = "increased" if curr_w > prev_w else "decreased"
                            warnings.append(
                                f"Indentation anomaly at line {curr_line_num} in {file_path}: "
                                f"indent {direction} from {prev_w} to {curr_w} "
                                f"(jump of {diff} spaces, threshold={threshold_val})"
                            )

                return warnings

            indent_warnings = validate_indentation_consistency(new_content, path)

            # Track heuristic edit history per file
            resolved_path_key = resolved.as_posix()
            self._heuristic_edit_counts[resolved_path_key] = \
                self._heuristic_edit_counts.get(resolved_path_key, 0) + 1

            has_trailing_newline = last_matched_line.endswith('\n') or last_matched_line.endswith('\r')
            if has_trailing_newline:
                if new_content and not (new_content.endswith('\n') or new_content.endswith('\r')):
                    if last_matched_line.endswith('\r\n'):
                        ending = '\r\n'
                    elif last_matched_line.endswith('\n'):
                        ending = '\n'
                    else:
                        ending = '\r'
                    new_content = new_content + ending
        elif match_mode == 'delete_and_insert':
            # ── delete_and_insert mode: line-range-based surgery ─────────────
            file_lines = file_content.splitlines(keepends=True)
            total_lines = len(file_lines)

            # Check for empty file — only "0" (append) makes sense on an empty file
            if total_lines == 0 and old_content.strip() != '0':
                return f"ERROR: Cannot use delete_and_insert mode on an empty file unless old_content='0' (append)."

            try:
                start_idx, end_idx = _parse_range(old_content, total_lines)
            except (ValueError, IndexError) as e:
                return f"ERROR: Invalid range '{old_content}' for delete_and_insert mode: {str(e)}"

            # Split file into before/deleted/after sections
            before = file_lines[:start_idx]
            after = file_lines[end_idx:]  # end_idx is exclusive (Python slice semantics)

            if new_content:
                inserted = new_content.splitlines(keepends=True)
                # Preserve line ending style from surrounding context for ALL inserted lines
                if after:
                    ref_ending = _detect_line_ending(after[0])
                    if not ref_ending and before:
                        ref_ending = _detect_line_ending(before[-1])
                    if ref_ending:
                        for i in range(len(inserted)):
                            # Normalize each line's ending to match file style
                            inserted[i] = inserted[i].rstrip('\r\n') + ref_ending
                file_lines = before + inserted + after
            else:
                # Delete-only: no insertion
                file_lines = before + after

            new_file_content = ''.join(file_lines)

        else:
            return f"ERROR: Invalid match_mode '{match_mode}'."

        description = f"Surgical edit to: {path} (mode: {match_mode})"
        tool_args = {'path': path, 'old_content': old_content, 'new_content': new_content, 'match_mode': match_mode}

        if not self._is_auto_approved(path, agent_name):
            approved, reason = self.request_user_approval(
                agent_name=agent_name,
                tool_name='edit_file',
                tool_args=tool_args,
                description=description,
            )
            if not approved:
                return f"REJECTED BY USER: {reason}"
            justification = reason
        else:
            justification = ""

        try:
            resolved = self._resolve_path(path, mode="rw")
            safe_agent = re.sub(r'[^a-zA-Z0-9_-]', '_', agent_name)
            backup_dir = self.base_dir / "logs" / "backups" / safe_agent
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"{resolved.name}.{int(time.time())}.bak"
            shutil.copy2(resolved, backup_path)
            try:
                backup_path_str = str(backup_path.relative_to(self.base_dir))
            except ValueError:
                backup_path_str = str(backup_path)

            file_content = resolved.read_text(encoding='utf-8')
            if match_mode == 'delete_and_insert':
                # new_file_content was already computed above — skip string replace
                pass
            else:
                new_file_content = file_content.replace(actual_old_content, new_content, 1)
            resolved.write_text(new_file_content, encoding='utf-8')

            self.file_ownership[str(resolved)] = agent_name

            res_msg = f"APPROVED: Edited {path}"
            if match_mode == 'delete_and_insert':
                res_msg += " (delete_and_insert mode)"
            elif match_mode in ('heuristic', 'heuristic_agnostic'):
                res_msg += f" (Heuristic match similarity: {match_ratio:.1%})"
                resolved_path_str = resolved.as_posix()
                edit_count = self._heuristic_edit_counts.get(resolved_path_str, 0)
                if edit_count >= 3:
                    res_msg += f" [NOTE: This file has been edited {edit_count} times in heuristic mode this session. Indentation drift may have accumulated.]"
                if indent_warnings:
                    for w in indent_warnings:
                        res_msg += f"\n  ⚠ {w}"
                res_msg += ". Please check the file to ensure the insertion was applied correctly."
            if justification:
                res_msg += f"\nSecurity Justification: {justification}"
            if backup_path_str:
                res_msg += f" (Backup saved to: {backup_path_str})"
            return res_msg
        except Exception as e:
            return f"ERROR: Approved but execution failed: {str(e)}"

    # ─── Re-indent ────────────────────────────────────────────────────────

    def re_indent(self, path: str, agent_name: str, lines: str, indent: int, indent_type: str, mode: str = "shift") -> str:
        """Re-indents a block of code in a file."""
        try:
            resolved = self._resolve_path(path, mode="rw")
        except Exception as e:
            return f"ERROR: {str(e)}"

        if not resolved.exists():
            return f"File not found for re-indentation: {path}"

        try:
            file_content = resolved.read_text(encoding='utf-8')
        except Exception as e:
            return f"ERROR: Failed to read file: {str(e)}"

        file_lines = file_content.splitlines(keepends=True)
        total_lines = len(file_lines)

        try:
            parts = lines.split(':')
            if len(parts) != 2:
                raise ValueError("lines must be in 'start:end' format")
            start_str, end_str = parts
            start = int(start_str) if start_str.strip() else 1
            end = int(end_str) if end_str.strip() else total_lines

            if start < 1:
                return f"ERROR: Line numbers must be >= 1. Got start={start}. Use 1-based format like '1:10'."
            if end < 1:
                return f"ERROR: Line numbers must be >= 1. Got end={end}. Use 1-based format like '1:10'."

            start = start - 1

            if start < 0:
                start += total_lines
            if end < 0:
                end += total_lines

            start = max(0, min(start, total_lines))
            end = max(0, min(end, total_lines))

            if start >= end:
                return f"ERROR: Invalid lines range: {lines} (start={start+1}, end={end}). Start must be less than end."
        except Exception as e:
            return f"ERROR: Invalid lines format '{lines}': {str(e)}. Use 1-based line range (e.g., '1:10')."

        block_lines = file_lines[start:end]

        def count_leading_ws(line: str) -> int:
            return len(line) - len(line.lstrip(' \t'))

        def count_visual_columns(line: str, tab_width: int) -> int:
            count = 0
            for ch in line:
                if ch == '\t':
                    count += tab_width
                elif ch == ' ':
                    count += 1
                else:
                    break
            return count

        ws_info_list = []
        ws_info_visual = []

        for line in block_lines:
            if not line.strip():
                ws_info_list.append(None)
                ws_info_visual.append(None)
            else:
                ws_count = count_leading_ws(line)
                visual_col = count_visual_columns(line, indent)
                stripped = line.lstrip(' \t')
                ws_info_list.append((ws_count, stripped))
                ws_info_visual.append((visual_col, stripped))

        ws_counts = [info[0] for info in ws_info_list if info is not None]
        min_visual_col = min((info[0] for info in ws_info_visual if info is not None), default=0)

        if not ws_counts:
            new_block_lines = block_lines
            base_trim = 0
        else:
            if mode == 'shift':
                base_trim = min(ws_counts)
            elif mode == 'flat':
                base_trim = None
            elif mode == 'convert':
                base_trim = min_visual_col
            else:
                return f"ERROR: Invalid mode '{mode}'. Choose 'shift', 'flat', or 'convert'."

            if indent < 0:
                return f"ERROR: indent must be non-negative, got {indent}."

            if mode == 'convert' and indent_type == 'tab' and indent == 0:
                return "ERROR: indent must be > 0 when indent_type='tab' in convert mode (used as tab width)."

            new_block_lines = []

            for i, info in enumerate(ws_info_list):
                if info is None:
                    original_line = block_lines[i]
                    suffix = ""
                    if original_line.endswith('\r\n'):
                        suffix = '\r\n'
                    elif original_line.endswith('\n'):
                        suffix = '\n'
                    elif original_line.endswith('\r'):
                        suffix = '\r'
                    new_block_lines.append(suffix)
                    continue

                ws_count, stripped_content = info

                if mode == 'flat':
                    relative_offset = 0
                elif mode == 'convert':
                    visual_col, _ = ws_info_visual[i]
                    relative_offset = visual_col - base_trim
                    total_visual_columns = indent + relative_offset

                    if indent_type == 'tab':
                        num_tabs = total_visual_columns // indent
                        remainder_spaces = total_visual_columns % indent
                        new_ws = '\t' * num_tabs + ' ' * remainder_spaces
                    else:
                        new_ws = ' ' * total_visual_columns

                    new_block_lines.append(new_ws + stripped_content)
                    continue
                else:
                    relative_offset = ws_count - base_trim

                total_indent_units = indent + relative_offset

                if indent_type == 'tab':
                    new_ws = '\t' * total_indent_units
                else:
                    new_ws = ' ' * total_indent_units

                new_block_lines.append(new_ws + stripped_content)

        # Track original whitespace unit for feedback message
        if ws_counts:
            first_non_blank_line = None
            for line in block_lines:
                if line.strip():
                    first_non_blank_line = line
                    break
            if first_non_blank_line:
                first_ws_len = len(first_non_blank_line) - len(first_non_blank_line.lstrip(' \t'))
                leading_ws = first_non_blank_line[:first_ws_len]
                original_ws_unit = 'tab' if '\t' in leading_ws else 'space'
            else:
                original_ws_unit = 'unknown'
        else:
            original_ws_unit = 'unknown'

        description = f"Re-indent block in: {path} (lines: {lines}, indent: {indent}, type: {indent_type}, mode: {mode})"
        tool_args = {'path': path, 'lines': lines, 'indent': indent, 'indent_type': indent_type, 'mode': mode}

        if not self._is_auto_approved(path, agent_name):
            approved, reason = self.request_user_approval(
                agent_name=agent_name,
                tool_name='re_indent',
                tool_args=tool_args,
                description=description,
            )
            if not approved:
                return f"REJECTED BY USER: {reason}"
            justification = reason
        else:
            justification = ""

        try:
            resolved = self._resolve_path(path, mode="rw")
            file_content = resolved.read_text(encoding='utf-8')
            file_lines = file_content.splitlines(keepends=True)

            safe_agent = re.sub(r'[^a-zA-Z0-9_-]', '_', agent_name)
            backup_dir = self.base_dir / "logs" / "backups" / safe_agent
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"{resolved.name}.{int(time.time())}.bak"
            shutil.copy2(resolved, backup_path)
            try:
                backup_path_str = str(backup_path.relative_to(self.base_dir))
            except ValueError:
                backup_path_str = str(backup_path)

            new_file_lines = list(file_lines)
            new_file_lines[start:end] = new_block_lines
            new_content_val = "".join(new_file_lines)
            resolved.write_text(new_content_val, encoding='utf-8')

            self.file_ownership[str(resolved)] = agent_name

            display_start = start + 1
            if mode == 'flat':
                res_msg = f"APPROVED: Re-indented lines {display_start}:{end} in {path}. Block had varying indents (trimmed all), now flattened to {indent} {indent_type}s."
            elif mode == 'convert':
                res_msg = f"APPROVED: Converted lines {display_start}:{end} in {path}. Used visual column alignment with tab width={indent}. Minimum indent was {base_trim} columns, re-aligned and converted to {indent_type}s."
            else:
                res_msg = f"APPROVED: Re-indented lines {display_start}:{end} in {path}. Block had base indent of {base_trim} {original_ws_unit}s, now indented to {indent} {indent_type}s."

            if justification:
                res_msg += f"\nSecurity Justification: {justification}"
            if backup_path_str:
                res_msg += f" (Backup saved to: {backup_path_str})"
            return res_msg
        except Exception as e:
            return f"ERROR: Approved but execution failed: {str(e)}"

    # ─── Delete file ──────────────────────────────────────────────────────

    def delete_file(self, path: str, agent_name: str) -> str:
        """Delete a file or directory — auto-approved for agent-owned files. Creates timestamped backup before deletion."""
        import os
        try:
            resolved = self._resolve_path(path, mode="rw")
        except Exception as e:
            return f"ERROR: {str(e)}"
        if not resolved.exists():
            return f"File not found: {path}"

        if not self._is_auto_approved(path, agent_name):
            description = f"Delete: {path}"
            approved, reason = self.request_user_approval(
                agent_name=agent_name,
                tool_name='delete_file',
                tool_args={'path': path},
                description=description,
            )
            if not approved:
                return f"REJECTED BY USER: {reason}"
            justification = reason
        else:
            justification = ""

        try:
            is_directory = resolved.is_dir()

            safe_agent = re.sub(r'[^a-zA-Z0-9_-]', '_', agent_name)
            backup_dir = self.base_dir / "logs" / "backups" / safe_agent
            backup_dir.mkdir(parents=True, exist_ok=True)

            timestamp = int(time.time())
            counter = 0
            while True:
                if counter == 0:
                    backup_filename = f"{resolved.name}.{timestamp}.bak"
                else:
                    backup_filename = f"{resolved.name}.{timestamp}_{counter}.bak"
                backup_path = backup_dir / backup_filename
                if not backup_path.exists():
                    break
                counter += 1

            try:
                shutil.move(resolved, backup_path)
            except Exception as move_err:
                if is_directory:
                    shutil.copytree(resolved, backup_path)
                    shutil.rmtree(resolved)
                else:
                    shutil.copy2(resolved, backup_path)
                    resolved.unlink()

            try:
                backup_path_str = str(backup_path.relative_to(self.base_dir))
            except ValueError:
                backup_path_str = str(backup_path)

            if str(resolved) in self.file_ownership:
                del self.file_ownership[str(resolved)]

            if is_directory:
                resolved_str = str(resolved) + os.sep
                keys_to_remove = [k for k in self.file_ownership.keys() if k.startswith(resolved_str)]
                for key in keys_to_remove:
                    del self.file_ownership[key]

            msg = f"APPROVED: Deleted {path}"
            if justification:
                msg += f"\nSecurity Justification: {justification}"
            msg += f". Backup created: {backup_path_str}"
            return msg
        except Exception as e:
            return f"ERROR: Approved but execution failed: {str(e)}"

    # ─── Copy file ────────────────────────────────────────────────────────

    def copy_file(self, source: str, destination: str, agent_name: str) -> str:
        """Copy a file — auto-approved if destination is new or agent-owned."""
        try:
            src_path = self._resolve_path(source, mode="ro")
            dest_path_check = self._resolve_path(destination, mode="rw")
        except Exception as e:
            return f"ERROR: {str(e)}"
        if not src_path.exists():
            return f"Source not found: {source}"

        if not self._is_auto_approved(destination, agent_name, creating_new=True):
            description = f"Copy: {source} → {destination}"
            approved, reason = self.request_user_approval(
                agent_name=agent_name,
                tool_name='copy_file',
                tool_args={'source': source, 'destination': destination},
                description=description,
            )
            if not approved:
                return f"REJECTED BY USER: {reason}"
            justification = reason
        else:
            justification = ""

        try:
            dest_path = self._resolve_path(destination, mode="rw")

            backup_path_str = ""
            if dest_path.exists():
                safe_agent = re.sub(r'[^a-zA-Z0-9_-]', '_', agent_name)
                backup_dir = self.base_dir / "logs" / "backups" / safe_agent
                backup_dir.mkdir(parents=True, exist_ok=True)

                timestamp = int(time.time())
                counter = 0
                while True:
                    if counter == 0:
                        backup_filename = f"{dest_path.name}.{timestamp}.bak"
                    else:
                        backup_filename = f"{dest_path.name}.{timestamp}_{counter}.bak"
                    backup_path = backup_dir / backup_filename
                    if not backup_path.exists():
                        break
                    counter += 1

                if dest_path.is_dir():
                    shutil.copytree(dest_path, backup_path)
                else:
                    shutil.copy2(dest_path, backup_path)

                try:
                    backup_path_str = str(backup_path.relative_to(self.base_dir))
                except ValueError:
                    backup_path_str = str(backup_path)

            if src_path.is_dir():
                shutil.copytree(src_path, dest_path, dirs_exist_ok=True)
            else:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dest_path)
            self.file_ownership[str(dest_path)] = agent_name
            msg = f"APPROVED: Copied {source} to {destination}"
            if justification:
                msg += f"\nSecurity Justification: {justification}"
            if backup_path_str:
                msg += f". Backup created: {backup_path_str}"
            return msg
        except Exception as e:
            return f"ERROR: Approved but execution failed: {str(e)}"

    # ─── Move file ────────────────────────────────────────────────────────

    def move_file(self, source: str, destination: str, agent_name: str) -> str:
        """Move a file — auto-approved if source is agent-owned."""
        try:
            src_path = self._resolve_path(source, mode="rw")
            dest_path_check = self._resolve_path(destination, mode="rw")
        except Exception as e:
            return f"ERROR: {str(e)}"
        if not src_path.exists():
            return f"Source not found: {source}"

        if not self._is_auto_approved(source, agent_name):
            description = f"Move: {source} → {destination}"
            approved, reason = self.request_user_approval(
                agent_name=agent_name,
                tool_name='move_file',
                tool_args={'source': source, 'destination': destination},
                description=description,
            )
            if not approved:
                return f"REJECTED BY USER: {reason}"
            justification = reason
        else:
            justification = ""

        try:
            dest_path = self._resolve_path(destination, mode="rw")

            backup_path_str = ""
            if dest_path.exists():
                safe_agent = re.sub(r'[^a-zA-Z0-9_-]', '_', agent_name)
                backup_dir = self.base_dir / "logs" / "backups" / safe_agent
                backup_dir.mkdir(parents=True, exist_ok=True)

                timestamp = int(time.time())
                counter = 0
                while True:
                    if counter == 0:
                        backup_filename = f"{dest_path.name}.{timestamp}.bak"
                    else:
                        backup_filename = f"{dest_path.name}.{timestamp}_{counter}.bak"
                    backup_path = backup_dir / backup_filename
                    if not backup_path.exists():
                        break
                    counter += 1

                if dest_path.is_dir():
                    shutil.copytree(dest_path, backup_path)
                else:
                    shutil.copy2(dest_path, backup_path)

                try:
                    backup_path_str = str(backup_path.relative_to(self.base_dir))
                except ValueError:
                    backup_path_str = str(backup_path)

                if dest_path.is_dir():
                    shutil.rmtree(dest_path)
                else:
                    dest_path.unlink()

            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src_path, dest_path)
            if str(src_path) in self.file_ownership:
                del self.file_ownership[str(src_path)]
            self.file_ownership[str(dest_path)] = agent_name
            msg = f"APPROVED: Moved {source} to {destination}"
            if justification:
                msg += f"\nSecurity Justification: {justification}"
            if backup_path_str:
                msg += f". Backup created: {backup_path_str}"
            return msg
        except Exception as e:
            return f"ERROR: Approved but execution failed: {str(e)}"

    # ─── Utilities ────────────────────────────────────────────────────────

    def get_file_owner(self, path: str) -> Optional[str]:
        """Get the owner of a file."""
        return self.file_ownership.get(path)