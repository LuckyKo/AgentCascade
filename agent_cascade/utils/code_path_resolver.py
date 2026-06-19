"""
Code Path Resolver for Code Interpreter Tool

This module provides automatic path translation from Windows host paths (e.g., N:\work\WD\AgentWorkspace)
to Docker container paths (/workspace) in Python code before execution. It uses regex-based 
detection to find and replace Windows-style absolute paths within string literals.

Path Mappings:
    Host Path                                    -> Container Path
    N:\work\WD\AgentWorkspace                    -> /workspace  
    N:\work\WD\AgentCascade_unified              -> /workspace/extra_rw_0
    
Example:
    N:\work\WD\AgentWorkspace\data.csv           -> /workspace/data.csv
    N:\work\WD\AgentCascade_unified\file.txt     -> /workspace/extra_rw_0/file.txt

Usage:
    from agent_cascade.utils.code_path_resolver import resolve_code_paths, build_path_resolution_notice
    
    code = "df = pd.read_csv(r'N:\\work\\WD\\AgentWorkspace\\data.csv')"
    resolved_code, count = resolve_code_paths(code)
    # resolved_code now has: df = pd.read_csv('/workspace/data.csv')

Author: PathAutoResolver (Fixed by Reviewer)
Date: 2026-06-19  
Version: 2.0 - Fixed regex bugs, consolidated into single location
"""

import re
from typing import Tuple


# ============================================================================
# PATH MAPPINGS CONFIGURATION
# ============================================================================

PATH_MAPPINGS = {
    "N:\\work\\WD\\AgentWorkspace": "/workspace",
    "N:\\work\\WD\\AgentCascade_unified": "/workspace/extra_rw_0",
}


def _resolve_single_path(path: str) -> str:
    """Resolve a single Windows path to its Docker container equivalent.
    
    Uses longest-prefix matching to handle nested directory structures correctly.
    e.g., AgentCascade_unified (longer) matches before AgentWorkspace (shorter).
    
    Args:
        path: A Windows-style absolute path string
        
    Returns:
        The resolved Docker container path, or original if no mapping found
        
    Examples:
        >>> _resolve_single_path(r"N:\work\WD\AgentWorkspace\data.csv")
        '/workspace/data.csv'
        
        >>> _resolve_single_path(r"N:\work\WD\AgentCascade_unified\file.txt")
        '/workspace/extra_rw_0/file.txt'
    """
    # Normalize to forward slashes for consistent matching
    normalized = path.replace("\\", "/")
    
    # Sort by length descending so longer prefixes match first
    sorted_mappings = sorted(
        PATH_MAPPINGS.items(), 
        key=lambda x: len(x[0].replace("\\", "/")), 
        reverse=True
    )
    
    for host_prefix, container_prefix in sorted_mappings:
        normalized_host = host_prefix.replace("\\", "/")
        
        if normalized.startswith(normalized_host):
            # Extract relative part after the mapped prefix
            relative_part = normalized[len(normalized_host):].lstrip("/")
            
            if relative_part:
                return f"{container_prefix}/{relative_part}"
            else:
                return container_prefix
    
    # No mapping found, return original path unchanged
    return path


# ============================================================================
# MAIN PATH RESOLUTION FUNCTION
# ============================================================================

def resolve_code_paths(code_str: str) -> Tuple[str, int]:
    """Resolve Windows host paths to Docker container paths in Python code.
    
    Scans the code string for string literals containing Windows-style absolute
    paths and replaces them with their Docker container equivalents.
    
    Handles:
        - Plain strings: "path" or 'path'
        - Raw strings: r"path", R"path"  
        - Bytes strings: b"path", B"path"
        - F-strings: f"path", F"path"
        - Combined prefixes: br"path", rf"path", etc.
        
    Skips:
        - Triple-quoted strings (too complex to handle safely)
        - Paths that are already in container format (/workspace/...)
    
    Args:
        code_str: Python code as a string
        
    Returns:
        Tuple[str, int]: 
            - Modified code with paths translated
            - Count of paths that were resolved
            
    Examples:
        >>> code = "df = pd.read_csv(r'N:\\work\\WD\\AgentWorkspace\\data.csv')"
        >>> resolved, count = resolve_code_paths(code)
        >>> count
        1
        >>> '/workspace/data.csv' in resolved
        True
        
        >>> # Idempotent - running twice gives same result
        >>> resolved2, count2 = resolve_code_paths(resolved)
        >>> count2
        0
        >>> resolved == resolved2
        True
    """
    if not code_str or not isinstance(code_str, str):
        return code_str, 0
    
    total_replaced = 0
    lines = code_str.split("\n")
    resolved_lines = []
    
    for line in lines:
        # Skip triple-quoted strings (they span multiple lines anyway)
        if '"""' in line or "'''" in line:
            resolved_lines.append(line)
            continue
        
        new_line, count = _process_single_line(line)
        total_replaced += count
        resolved_lines.append(new_line)
    
    return "\n".join(resolved_lines), total_replaced


def _process_single_line(line: str) -> Tuple[str, int]:
    """Process a single line of code to resolve paths in string literals.
    
    Uses manual parsing instead of complex regex to properly handle:
    - Plain strings (no prefix)
    - Strings with prefixes (r, R, b, B, f, F, br, rf, etc.)
    - Escaped quotes within strings
    - Comments (everything after # is skipped)
    
    Args:
        line: A single line of Python code
        
    Returns:
        Tuple[str, int]: Modified line and count of replacements made
    """
    replaced_count = 0
    result = []
    i = 0
    
    while i < len(line):
        # Handle comments: everything after # outside a string is a comment
        if line[i] == '#':
            # Append the rest of the line as-is (it's a comment)
            result.append(line[i:])
            break
        
        # Check if we're at the start of a string literal
        # Look for optional prefix followed by quote
        
        # Skip ahead to find potential string start
        prefix_start = i
        prefix_end = i
        
        # Collect prefix characters (r, R, b, B, f, F)
        while prefix_end < len(line) and line[prefix_end] in 'rRbBfF':
            prefix_end += 1
        
        # Check if there's a quote after the prefix
        if prefix_end < len(line) and line[prefix_end] in '"\'':
            prefix = line[prefix_start:prefix_end]
            quote_char = line[prefix_end]
            is_raw = 'r' in prefix.lower()
            
            # Find the closing quote, handling escapes
            # In raw strings, \" is NOT an escape — the backslash stays
            # and the quote terminates the string.
            # In non-raw strings, \" IS an escape — skip both chars.
            j = prefix_end + 1
            while j < len(line):
                if line[j] == '\\' and not is_raw and j + 1 < len(line):
                    j += 2  # Skip escaped character (only for non-raw strings)
                    continue
                elif line[j] == quote_char:
                    # Found closing quote - extract the full string
                    full_string = line[prefix_start:j+1]
                    
                    # Process this string to resolve paths
                    new_string, count = _process_string_literal(full_string)
                    replaced_count += count
                    result.append(new_string)
                    
                    i = j + 1
                    break
                else:
                    j += 1
            else:
                # No closing quote found - treat as regular character
                result.append(line[i])
                i += 1
        else:
            # Not a string start, just copy the character
            result.append(line[i])
            i += 1
    
    return ''.join(result), replaced_count


def _process_string_literal(string_literal: str) -> Tuple[str, int]:
    """Process a single string literal to resolve Windows paths.
    
    Args:
        string_literal: The full string including prefix and quotes
        
    Returns:
        Tuple[str, int]: Modified string and count of replacements made
    """
    replaced_count = 0
    
    # Extract prefix (r, R, b, B, f, F or combinations)
    prefix_match = re.match(r'^([rRbBfF]*)(["\'])', string_literal)
    
    if not prefix_match:
        return string_literal, 0
    
    prefix = prefix_match.group(1) or ""
    quote_char = prefix_match.group(2)
    
    # Extract content between quotes
    content_start = len(prefix) + 1
    remaining = string_literal[content_start:]
    
    # Find closing quote (should be the last character for a valid string literal)
    if not remaining.endswith(quote_char):
        return string_literal, 0
    
    content = remaining[:-1]  # Remove closing quote
    
    # Match Windows absolute paths within the string content
    # Requires backslash or forward slash after drive letter to prevent false matches like "C:foo"
    windows_path_pattern = re.compile(r'[A-Za-z]:[\\/][^\r\n\'"]*')
    
    def replace_single_path(path_match):
        nonlocal replaced_count
        
        matched_path = path_match.group(0)
        
        # Skip if it's already a container path (starts with /)
        if matched_path.startswith("/"):
            return matched_path
        
        resolved_path = _resolve_single_path(matched_path)
        
        # Only count if actually changed and was a Windows-style path
        if resolved_path != matched_path:
            replaced_count += 1
        
        return resolved_path
    
    new_content = windows_path_pattern.sub(replace_single_path, content)
    
    # Reconstruct the string with original prefix and quotes
    return f"{prefix}{quote_char}{new_content}{quote_char}", replaced_count


# ============================================================================
# FEEDBACK NOTIFICATION
# ============================================================================

def build_path_resolution_notice(count: int) -> str:
    """Build a feedback message indicating how many paths were auto-resolved.
    
    Args:
        count: Number of paths that were resolved
        
    Returns:
        Formatted string for display, empty if count is 0
        
    Examples:
        >>> build_path_resolution_notice(0)
        ''
        
        >>> build_path_resolution_notice(1)
        '[SYSTEM] 1 path(s) auto-resolved before execution. If this interfered with your intended code, please use fix_paths=false'
        
        >>> build_path_resolution_notice(5)
        '[SYSTEM] 5 path(s) auto-resolved before execution. If this interfered with your intended code, please use fix_paths=false'
    """
    if count <= 0:
        return ""
    
    return f"[SYSTEM] {count} path(s) auto-resolved before execution. If this interfered with your intended code, please use fix_paths=false"


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = [
    "resolve_code_paths",
    "build_path_resolution_notice",
    "PATH_MAPPINGS",
]