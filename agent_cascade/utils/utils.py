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

import base64
import copy
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import sys
import time
import traceback
import urllib.parse
from collections import OrderedDict
from io import BytesIO
from typing import Any, List, Literal, Optional, Tuple, Union

import json5
import numpy as np
import requests
import soundfile as sf
from pydantic import BaseModel

from agent_cascade.llm.schema import ASSISTANT, DEFAULT_SYSTEM_MESSAGE, FUNCTION, ROLE, SYSTEM, USER, ContentItem, Message
from agent_cascade.log import logger
from agent_cascade.settings import IMAGE_TOKEN_ESTIMATE

# Max length for function/tool call arguments before truncation (shared across utils and agent_invoker)
MAX_FC_ARGS_LEN = 2048

# Default timeout for HTTP requests (seconds)
DEFAULT_REQUEST_TIMEOUT = 30


# ── Message Field Accessor Helpers (consolidated from execution_engine, handler, api_server) ──

def msg_field(msg, field_name: str, default=None):
    """Get a field from a message, handling both dict and Message objects.
    
    Replaces duplicated _msg_field / _msg_role / _msg_content / _get_msg_role patterns
    that existed across execution_engine.py, compression/handler.py, and api_server.py.
    
    Args:
        msg: Message object or dict with message fields
        field_name: Field name to access ('role', 'content', 'extra', etc.)
        default: Default value if field not found
        
    Returns:
        Field value or default
    """
    return msg.get(field_name, default) if isinstance(msg, dict) else getattr(msg, field_name, default)


def msg_set(msg, field_name: str, value) -> None:
    """Set a field on a message, handling both dict and Message objects.
    
    Args:
        msg: Message object or dict with message fields
        field_name: Field name to set ('role', 'content', etc.)
        value: Value to assign
    """
    if isinstance(msg, dict):
        msg[field_name] = value
    else:
        setattr(msg, field_name, value)


def msg_has_field(msg, field_name: str) -> bool:
    """Check if a message has a field, handling both dict and Message objects.
    
    Args:
        msg: Message object or dict with message fields
        field_name: Field name to check for
        
    Returns:
        True if the field exists on the message
    """
    return field_name in msg if isinstance(msg, dict) else hasattr(msg, field_name)


def append_signal_handler(sig, handler):
    """
    Installs a new signal handler while preserving any existing handler.
    If an existing handler is present, it will be called _after_ the new handler.
    """

    old_handler = signal.getsignal(sig)
    if not callable(old_handler):
        old_handler = None
        if sig == signal.SIGINT:

            def old_handler(*args, **kwargs):
                raise KeyboardInterrupt
        elif sig == signal.SIGTERM:

            def old_handler(*args, **kwargs):
                raise SystemExit

    def new_handler(*args, **kwargs):
        handler(*args, **kwargs)
        if old_handler is not None:
            old_handler(*args, **kwargs)

    signal.signal(sig, new_handler)


def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # doesn't even have to be reachable
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip


def hash_sha256(text: str) -> str:
    hash_object = hashlib.sha256(text.encode())
    key = hash_object.hexdigest()
    return key


def print_traceback(is_error: bool = True):
    tb = ''.join(traceback.format_exception(*sys.exc_info(), limit=3))
    if is_error:
        logger.error(tb)
    else:
        logger.warning(tb)


from agent_cascade.utils.thinking_block import (
    _THINK_BLOCK_RE, _THINK_BLOCK_UNCLOSED_RE,
    _THINK_BLOCK_BRACKET_RE,
    _MARKDOWN_CODE_RE, _TRIPLE_QUOTE_RE,
    _JSON_STRING_RE, CHINESE_CHAR_RE, _IMAGE_DATA_RE as IMAGE_REGEX
)


def has_chinese_chars(data: Any) -> bool:
    text = f'{data}'
    return bool(CHINESE_CHAR_RE.search(text))


def has_chinese_messages(messages: List[Union[Message, dict, list, bool, None]], check_roles: Tuple[str] = (SYSTEM, USER)) -> bool:
    """Check if any message in the list contains Chinese characters.
    
    Skips non-dict/non-Message items (booleans, None, lists) that can leak via JSON parsing or logger recovery.
    Follows the same defensive pattern as get_history_stats().
    """
    for m in messages:
        # Defensive type checking: skip unexpected types that can leak into messages list
        if m is None:
            logger.debug("has_chinese_messages: skipping None value in messages list")
            continue
        elif isinstance(m, bool):
            # Check bool BEFORE int since bool is a subclass of int in Python
            logger.debug(f"has_chinese_messages: skipping unexpected bool value in messages list: {m}")
            continue
        elif isinstance(m, list):
            logger.debug("has_chinese_messages: skipping unexpected list item in messages list")
            continue
        elif not isinstance(m, (dict, Message)):
            logger.debug(f"has_chinese_messages: skipping unexpected type {type(m).__name__} in messages list")
            continue
        
        # Extract role safely based on type
        if isinstance(m, dict):
            role = m.get('role')
            content = m.get('content', '')
        else:  # Message object
            role = getattr(m, 'role', None)
            content = getattr(m, 'content', '')
        
        if role in check_roles:
            if has_chinese_chars(content):
                return True
    return False


def get_basename_from_url(path_or_url: str) -> str:
    if re.match(r'^[A-Za-z]:\\', path_or_url):
        # "C:\\a\\b\\c" -> "C:/a/b/c"
        path_or_url = path_or_url.replace('\\', '/')

    # "/mnt/a/b/c" -> "c"
    # "https://github.com/here?k=v" -> "here"
    # "https://github.com/" -> ""
    basename = urllib.parse.urlparse(path_or_url).path
    basename = os.path.basename(basename)
    basename = urllib.parse.unquote(basename)
    basename = basename.strip()

    # "https://github.com/" -> "" -> "github.com"
    if not basename:
        basename = [x.strip() for x in path_or_url.split('/') if x.strip()][-1]

    return basename


def is_http_url(path_or_url: str) -> bool:
    if path_or_url.startswith('https://') or path_or_url.startswith('http://'):
        return True
    return False


def is_image(path_or_url: str) -> bool:
    filename = get_basename_from_url(path_or_url).lower()
    for ext in ['jpg', 'jpeg', 'png', 'webp']:
        if filename.endswith(ext):
            return True
    return False


def sanitize_chrome_file_path(file_path: str) -> str:
    if os.path.exists(file_path):
        return file_path

    # Dealing with "file:///...":
    new_path = urllib.parse.urlparse(file_path)
    new_path = urllib.parse.unquote(new_path.path)
    new_path = sanitize_windows_file_path(new_path)
    if os.path.exists(new_path):
        return new_path

    return sanitize_windows_file_path(file_path)


def sanitize_windows_file_path(file_path: str) -> str:
    # For Linux and macOS.
    if os.path.exists(file_path):
        return file_path

    # For native Windows, drop the leading '/' in '/C:/'
    win_path = file_path
    if win_path.startswith('/'):
        win_path = win_path[1:]
    if os.path.exists(win_path):
        return win_path

    # For Windows + WSL.
    if re.match(r'^[A-Za-z]:/', win_path):
        wsl_path = f'/mnt/{win_path[0].lower()}/{win_path[3:]}'
        if os.path.exists(wsl_path):
            return wsl_path

    # For native Windows, replace / with \.
    win_path = win_path.replace('/', '\\')
    if os.path.exists(win_path):
        return win_path

    return file_path


def save_url_to_local_work_dir(url: str, save_dir: str, save_filename: str = '') -> str:
    if not save_filename:
        save_filename = get_basename_from_url(url)
    new_path = os.path.join(save_dir, save_filename)
    if os.path.exists(new_path):
        os.remove(new_path)
    logger.info(f'Downloading {url} to {new_path}...')
    start_time = time.time()
    if not is_http_url(url):
        url = sanitize_chrome_file_path(url)
        shutil.copy(url, new_path)
    else:
        headers = {
            'User-Agent':
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'
        }
        try:
            response = requests.get(url, headers=headers, timeout=DEFAULT_REQUEST_TIMEOUT)
            response.raise_for_status()
            with open(new_path, 'wb') as file:
                file.write(response.content)
        except requests.RequestException as e:
            raise ValueError(f'Can not download this file. Please check your network or the file link. (Error: {e})')
    end_time = time.time()
    logger.info(f'Finished downloading {url} to {new_path}. Time spent: {end_time - start_time} seconds.')
    return new_path


def save_text_to_file(path: str, text: str) -> None:
    with open(path, 'w', encoding='utf-8') as fp:
        fp.write(text)


def read_text_from_file(path: str) -> str:
    try:
        with open(path, 'r', encoding='utf-8') as file:
            file_content = file.read()
    except UnicodeDecodeError:
        print_traceback(is_error=False)
        from charset_normalizer import from_path
        results = from_path(path)
        file_content = str(results.best())
    return file_content


def contains_html_tags(text: str) -> bool:
    pattern = r'<(p|span|div|li|html|script)[^>]*?'
    return bool(re.search(pattern, text))


def get_content_type_by_head_request(path: str) -> str:
    try:
        response = requests.head(path, timeout=5)
        content_type = response.headers.get('Content-Type', '')
        return content_type
    except requests.RequestException:
        return 'unk'


def get_file_type(path: str) -> Literal['pdf', 'docx', 'pptx', 'txt', 'html', 'csv', 'tsv', 'xlsx', 'xls', 'unk']:
    f_type = get_basename_from_url(path).split('.')[-1].lower()
    if f_type in ['pdf', 'docx', 'pptx', 'csv', 'tsv', 'xlsx', 'xls', 'md']:
        # Specially supported file types
        return f_type

    if is_http_url(path):
        # The HTTP header information for the response is obtained by making a HEAD request to the target URL,
        # where the Content-type field usually indicates the Type of Content to be returned
        content_type = get_content_type_by_head_request(path)
        if 'application/pdf' in content_type:
            return 'pdf'
        elif 'application/msword' in content_type:
            return 'docx'

        # Assuming that the URL is HTML by default,
        # because the file downloaded by the request may contain html tags
        return 'html'
    else:
        # Determine by reading local HTML file
        try:
            content = read_text_from_file(path)
        except Exception:
            print_traceback()
            return 'unk'

        if contains_html_tags(content):
            return 'html'
        else:
            return 'txt'


def extract_urls(text: str) -> List[str]:
    pattern = re.compile(r'https?://\S+')
    urls = re.findall(pattern, text)
    return urls


def extract_markdown_urls(md_text: str) -> List[str]:
    pattern = r'!?\[[^\]]*\]\(([^\)]+)\)'
    urls = re.findall(pattern, md_text)
    return urls


def extract_code(text: Union[str, dict]) -> str:
    if isinstance(text, dict):
        return text.get('code', '')

    # Match triple backtick blocks first
    triple_match = re.search(r'```[^\n]*\n(.+?)```', text, re.DOTALL)
    if triple_match:
        text = triple_match.group(1)
    else:
        try:
            text = json5.loads(text)['code']
        except Exception:
            pass
    # If no code blocks found, return original text
    return text


def repair_invalid_json(text: str) -> str:
    """
    Attempt to repair common LLM JSON mistakes before parsing.
    Specifically handles triple-quoted strings and unescaped newlines in values.
    """
    import re

    # 1. Handle triple quotes in values: """content""" -> "content" (with escaped newlines)
    repaired = re.sub(r'(":\s*)"""(.*?)"""(?=[,}\s])',
                      lambda m: m.group(1) + json.dumps(m.group(2).replace('\\n', '\n')).replace('\\\\n', '\\n'),
                      text, flags=re.DOTALL)

    # 2. Handle literal newlines in double-quoted values (very common failure mode)
    def escape_newlines(match):
        prefix = match.group(1)
        content = match.group(2)
        # Escape any literal newlines that shouldn't be there
        return prefix + '"' + content.replace('\n', '\\n') + '"'

    # This regex matches:
    # (:\s*)           -> The colon and optional whitespace before the value
    # "                -> The opening quote
    # ((?:[^"\\]|\\.)*?) -> The content: any char except " or \, OR any escaped char (\.)
    # "                -> The closing quote
    # (?=\s*[,}\]])     -> Lookahead for a delimiter to ensure it's a value end
    repaired = re.sub(r'(:\s*)"((?:[^"\\]|\\.)*?)"(?=\s*[,}\]])', escape_newlines, repaired, flags=re.DOTALL)

    return repaired


def json_loads(text: str) -> Union[dict, str]:
    import logging
    import json5

    _logger = logging.getLogger(__name__)
    original_text = text.strip()
    
    # 0. Strip thinking blocks first to avoid them interfering with parsing
    # if they contain {} markers or quotes.
    # CRITICAL: We only strip from the START using anchored regexes.
    # We do this iteratively to handle multiple tags.
    changed = True
    while changed:
        changed = False
        lower_text = original_text.lower()
        if '<think' in lower_text or '<thought' in lower_text:
            new_text = _THINK_BLOCK_RE.sub('', original_text, count=1)
            if new_text != original_text:
                original_text = new_text
                changed = True
        
        if not changed and ('[think' in lower_text or '[thought' in lower_text):
            new_text = _THINK_BLOCK_BRACKET_RE.sub('', original_text, count=1)
            if new_text != original_text:
                original_text = new_text
                changed = True
    
    original_text = original_text.strip()
    # 1. Try parsing as-is (handles most cases including those with backticks inside)
    try:
        return json5.loads(original_text)
    except Exception:
        _logger.debug("JSON parse attempt 1 failed (direct)")

    # 2. Try stripping markdown code blocks (handles cases where the whole response is wrapped)
    text = original_text
    if '```' in text:
        match = _MARKDOWN_CODE_RE.search(text)
        if match:
            text = match.group(1).strip()
        else:
            text = text.replace('```json', '').replace('```', '').strip()

    try:
        return json5.loads(text)
    except Exception:
        _logger.debug("JSON parse attempt 2 failed (markdown strip)")

    # 3. Try repairing common mistakes (triple quotes, literal newlines)
    try:
        repaired = repair_invalid_json(original_text)
        return json5.loads(repaired)
    except Exception:
        _logger.debug("JSON parse attempt 3 failed (repair)")

    # 4. Try extracting just the JSON object between the first { and last }
    try:
        start_idx = text.find('{')
        end_idx = text.rfind('}')
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_str = text[start_idx:end_idx+1]
            repaired = repair_invalid_json(json_str)
            # Use repaired string if extraction alone wasn't enough
            try:
                return json5.loads(json_str)
            except Exception:
                return json5.loads(repaired)
    except Exception:
        _logger.debug("Failed to parse JSON from extracted block")

    # 5. Try repairing the STRIPPED text as a last resort
    try:
        repaired = repair_invalid_json(text)
        return json5.loads(repaired)
    except Exception:
        _logger.debug("Failed to parse JSON after repair")

    # 6. Return stripped original text as string fallback (for non-JSON input)
    return text.strip()


class PydanticJSONEncoder(json.JSONEncoder):

    def default(self, obj):
        if isinstance(obj, BaseModel):
            return obj.model_dump()
        return super().default(obj)


def json_dumps_pretty(obj: dict, ensure_ascii=False, indent=2, **kwargs) -> str:
    return json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent, cls=PydanticJSONEncoder, **kwargs)


def json_dumps_compact(obj: dict, ensure_ascii=False, indent=None, **kwargs) -> str:
    return json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent, cls=PydanticJSONEncoder, **kwargs)


def format_as_multimodal_message(
    msg: Message,
    add_upload_info: bool,
    add_multimodel_upload_info: bool,
    add_audio_upload_info: bool,
    lang: Literal['auto', 'en', 'zh'] = 'auto',
) -> Message:
    assert msg.role in (USER, ASSISTANT, SYSTEM, FUNCTION)
    content: List[ContentItem] = []
    if isinstance(msg.content, str):  # if text content
        content = [ContentItem(text=msg.content)]
    elif isinstance(msg.content, list):  # if multimodal content
        files = []
        for item in msg.content:
            k, v = item.get_type_and_value()
            if k in ('text', 'image', 'audio', 'video'):
                content.append(item)
            if k == 'file':
                # Move 'file' out of 'content' since it's not natively supported by models
                files.append((v, k))
            if add_multimodel_upload_info and k in ('image', 'video'):
                # Indicate the image name
                if isinstance(v, str):
                    files.append((v, k))
                elif isinstance(v, list):
                    for _v in v:
                        files.append((_v, k))
                else:
                    raise TypeError

            if add_audio_upload_info and k == 'audio':
                if isinstance(v, str):
                    files.append((v, k))
                elif isinstance(v, dict):
                    files.append((v['data'], k))
                else:
                    raise TypeError

        if add_upload_info and files and (msg.role in (SYSTEM, USER)):
            if lang == 'auto':
                has_zh = has_chinese_chars(msg)
            else:
                has_zh = (lang == 'zh')
            upload = []
            for f, k in [(get_basename_from_url(f), k) for f, k in files]:
                if k == 'image':
                    if has_zh:
                        upload.append(f'![图片]({f})')
                    else:
                        upload.append(f'![image]({f})')
                elif k == 'video':
                    if has_zh:
                        upload.append(f'![视频]({f})')
                    else:
                        upload.append(f'![video]({f})')
                elif k == 'audio':
                    if has_zh:
                        upload.append(f'![音频]({f})')
                    else:
                        upload.append(f'![audio]({f})')
                else:
                    if has_zh:
                        upload.append(f'[文件]({f})')
                    else:
                        upload.append(f'[file]({f})')
            if upload:
                upload = ' '.join(upload)
                if msg.role in (SYSTEM, USER):
                    if has_zh:
                        upload = f'（上传了 {upload}）'
                    else:
                        upload = f'(Uploaded {upload}) '
                elif msg.role in (ASSISTANT, FUNCTION):
                    if has_zh:
                        upload = f'{upload}'
                    else:
                        upload = f'{upload}'
                # Check and avoid adding duplicate upload info
                upload_info_already_added = False
                for item in content:
                    if item.text and (upload in item.text):
                        upload_info_already_added = True
                if not upload_info_already_added:
                    if msg.role == ASSISTANT or msg.role == FUNCTION:
                        content = [ContentItem(text=upload)]
                    else:
                        content = [ContentItem(text=upload)] + content
    else:
        raise TypeError
    msg = Message(role=msg.role,
                  content=content,
                  reasoning_content=msg.reasoning_content,
                  name=msg.name if msg.role == FUNCTION else None,
                  function_call=msg.function_call,
                  extra=msg.extra)
    return msg


def format_as_text_message(
    msg: Message,
    add_upload_info: bool,
    lang: Literal['auto', 'en', 'zh'] = 'auto',
) -> Message:
    msg = format_as_multimodal_message(msg,
                                       add_upload_info=add_upload_info,
                                       add_multimodel_upload_info=add_upload_info,
                                       add_audio_upload_info=add_upload_info,
                                       lang=lang)
    text = ''
    for item in msg.content:
        if item.type == 'text':
            text += item.value
        elif item.type == 'image':
            caption = getattr(item, 'caption', None) or (item.get('caption') if isinstance(item, dict) else None)
            if caption:
                text += f'[Image: {caption}]'
            else:
                text += '[Image]'
        elif item.type == 'audio':
            text += '[Audio]'
        elif item.type == 'video':
            text += '[Video]'
    msg.content = text
    return msg


def save_audio_to_file(base_64: str, file_name: str):
    wav_bytes = base64.b64decode(base_64)
    audio_np = np.frombuffer(wav_bytes, dtype=np.int16)
    sf.write(file_name, audio_np, samplerate=24000)


def _msg_field_or_extra(msg, field_name):
    """Extract a field from msg, checking direct attribute/dict key first, then the extra dict.
    
    Shared helper for accessing message fields that may live on the message directly
    or nested inside an 'extra' dict (common for non-schema fields like tool_calls).
    
    Args:
        msg: Message object or dict with message fields.
        field_name: Field name to look up.
        
    Returns:
        Field value, or None if not found.
    """
    if isinstance(msg, dict):
        val = msg.get(field_name)
        if val is None and 'extra' in msg and isinstance(msg['extra'], dict):
            val = msg['extra'].get(field_name)
        return val
    else:
        val = getattr(msg, field_name, None)
        # Also check extra dict on Message objects for non-schema fields like tool_calls
        if val is None:
            extra = getattr(msg, 'extra', None)
            if isinstance(extra, dict):
                val = extra.get(field_name)
        return val


def _format_tool_calls_for_text(msg):
    """Format function_call and tool_calls from an assistant message into readable text.
    
    Shared helper used by both extract_text_from_message() and agent_invoker._format_messages_for_summary().
    Handles dict and Message objects, legacy function_call and modern tool_calls array formats.
    Arguments exceeding MAX_FC_ARGS_LEN are truncated to prevent context blowup.
    
    Args:
        msg: Message object or dict with message fields.
        
    Returns:
        Formatted text like "[TOOL CALL: name(args)]" or empty string if no tool calls found.
    """
    # Check legacy single function_call (takes highest priority)
    fc = _msg_field_or_extra(msg, 'function_call')
    
    if fc is not None:
        if isinstance(fc, dict):
            fc_name = fc.get('name', 'unknown')
            fc_args = fc.get('arguments', '')
        else:
            fc_name = getattr(fc, 'name', 'unknown')
            fc_args = getattr(fc, 'arguments', '')
        
        # Truncate large arguments to avoid context blowup
        if isinstance(fc_args, str) and len(fc_args) > MAX_FC_ARGS_LEN:
            fc_args = fc_args[:MAX_FC_ARGS_LEN] + '... [TRUNCATED]'
        
        return f"[TOOL CALL: {fc_name}({fc_args})]"
    
    # Check modern tool_calls array
    tc = _msg_field_or_extra(msg, 'tool_calls')
    
    if tc is not None and isinstance(tc, list) and len(tc) > 0:
        call_parts = []
        for tc_item in tc:
            # Extract function name and arguments from dict or object
            if isinstance(tc_item, dict):
                tc_func = tc_item.get('function', {})
                if isinstance(tc_func, dict):
                    tc_name = tc_func.get('name', 'unknown')
                    tc_args = tc_func.get('arguments', '')
                else:
                    tc_name = getattr(tc_func, 'name', 'unknown')
                    tc_args = getattr(tc_func, 'arguments', '')
            elif hasattr(tc_item, 'function'):
                tc_func = tc_item.function
                if hasattr(tc_func, 'name'):
                    tc_name = tc_func.name
                    tc_args = tc_func.arguments if hasattr(tc_func, 'arguments') else ''
                else:
                    tc_name = 'unknown'
                    tc_args = ''
            else:
                tc_name = 'unknown'
                tc_args = ''
            
            # Truncate large arguments to avoid context blowup
            if isinstance(tc_args, str) and len(tc_args) > MAX_FC_ARGS_LEN:
                tc_args = tc_args[:MAX_FC_ARGS_LEN] + '... [TRUNCATED]'
            
            call_parts.append(f"[TOOL CALL: {tc_name}({tc_args})]")
        
        return "\n".join(call_parts)

    return ""


def _reasoning_to_text(rc, truncate=True) -> str:
    """Convert reasoning_content to a text string, handling both str and list (multi-modal) types.

    Args:
        rc: reasoning_content value — can be a string or a list of ContentItems.
        truncate: If True, truncate output at MAX_FC_ARGS_LEN to prevent context blowup.

    Returns:
        Plain text string (empty if no reasoning content found).
    """
    # Handle None/missing gracefully
    if not rc and rc != '':
        return ''

    if isinstance(rc, str):
        result = rc.strip()
    elif isinstance(rc, list):
        parts = []
        for item in rc:
            if isinstance(item, dict):
                text = item.get('text', '') or ''
            else:
                text = getattr(item, 'text', None) or ''
            if text:
                parts.append(str(text))
        result = ' '.join(parts).strip() if parts else ''
    else:
        result = rc

    # Truncate to prevent context blowup (same limit as function_call args)
    if truncate and isinstance(result, str) and len(result) > MAX_FC_ARGS_LEN:
        result = result[:MAX_FC_ARGS_LEN] + '... [TRUNCATED]'

    return result


def extract_text_from_message(
    msg: Union[Message, dict, list, bool, None],
    add_upload_info: bool,
    lang: Literal['auto', 'en', 'zh'] = 'auto',
) -> str:
    """Extract text content from a message with defensive type checking.
    
    BUG FIX: Handle unexpected types (especially booleans and lists) that may leak into 
    conversation history via JSON parsing or logger recovery paths.
    
    Args:
        msg: Message object, dict, list, bool, or None (defensive handling).
        add_upload_info: Whether to include upload info in text.
        lang: Language for formatting ('auto', 'en', 'zh').
        
    Returns:
        Extracted text content, or empty string for unexpected types.
    """
    from agent_cascade.log import logger

    # Handle None gracefully (defensive check)
    if msg is None:
        logger.debug("extract_text_from_message received None (returning empty)")
        return ""
    
    # Handle list values gracefully (defensive check)
    if isinstance(msg, list):
        logger.debug(f"extract_text_from_message received a list (returning empty): {str(msg)[:50]}")
        return ""
    
    # Handle boolean values gracefully (defensive check - must come before generic isinstance checks since bool is a subclass of int)
    if isinstance(msg, bool):
        logger.debug(f"extract_text_from_message received a bool (returning empty): {msg}")
        return ""
    
    # Handle dict by converting to Message
    if isinstance(msg, dict):
        msg = Message(**msg)
    
    # Now msg should be a Message object - extract content safely
    if not msg_has_field(msg, 'content'):
        logger.debug(f"extract_text_from_message: message has no 'content' attribute: {type(msg)}")
        return ""
        
    if isinstance(msg.content, list):
        text = format_as_text_message(msg, add_upload_info=add_upload_info, lang=lang).content
    elif isinstance(msg.content, str):
        text = msg.content
    else:
        # Handle other unexpected content types gracefully instead of raising
        logger.debug(f"extract_text_from_message: unexpected content type {type(msg.content).__name__}")
        return ""

    # For assistant messages with empty/missing text, check reasoning first, then tool calls
    if not text.strip() and msg.role == 'assistant':
        # Check reasoning_content (OpenAI-style thinking/reasoning field)
        rc = _msg_field_or_extra(msg, 'reasoning_content')
        rc_text = _reasoning_to_text(rc)
        if rc_text:
            text = f"[THOUGHT: {rc_text}]"
        else:
            # Fall back to tool calls
            text = _format_tool_calls_for_text(msg)

    return text.strip()


def _get_msg_content(msg):
    """Extract content from a message, handling both Message objects and dicts.
    
    Returns the content attribute/value, or None if not present.
    """
    if isinstance(msg, dict):
        return msg.get('content')
    return getattr(msg, 'content', None)


def _get_item_attr(item, attr_name: str):
    """Extract an attribute from a ContentItem object or dict."""
    if isinstance(item, dict):
        return item.get(attr_name)
    return getattr(item, attr_name, None)


def extract_files_from_messages(messages: List[Union[dict, Message]], include_images: bool) -> List[str]:
    files = []
    for msg in messages:
        content = _get_msg_content(msg)
        if isinstance(content, list):
            for item in content:
                file_val = _get_item_attr(item, 'file')
                if file_val and file_val not in files:
                    files.append(file_val)
                if include_images:
                    image_val = _get_item_attr(item, 'image')
                    if image_val and image_val not in files:
                        files.append(image_val)
    return files


def extract_images_from_messages(messages: List[Union[dict, Message]]) -> List[str]:
    images = []
    for msg in messages:
        content = _get_msg_content(msg)
        if isinstance(content, list):
            for item in content:
                image_val = _get_item_attr(item, 'image')
                if image_val and image_val not in images:
                    images.append(image_val)
    return images


def merge_generate_cfgs(base_generate_cfg: Optional[dict], new_generate_cfg: Optional[dict]) -> dict:
    generate_cfg: dict = copy.deepcopy(base_generate_cfg or {})
    if new_generate_cfg:
        for k, v in new_generate_cfg.items():
            if k == 'stop':
                stop = generate_cfg.get('stop', [])
                stop = stop + [s for s in v if s not in stop]
                generate_cfg['stop'] = stop
            else:
                generate_cfg[k] = v
    return generate_cfg


def build_text_completion_prompt(
    messages: List[Message],
    allow_special: bool = False,
    default_system: str = DEFAULT_SYSTEM_MESSAGE,
) -> str:
    logger.warning('Support for `build_text_completion_prompt` is deprecated. '
                   'Please use `tokenizer.apply_chat_template(...)` instead to construct the prompt from messages.')

    im_start = '<|im_start|>'
    im_end = '<|im_end|>'

    if messages and messages[0].role == SYSTEM:
        sys = messages[0].content
        assert isinstance(sys, str)
        prompt = f'{im_start}{SYSTEM}\n{sys}{im_end}'
        messages = messages[1:]
    elif default_system:
        prompt = f'{im_start}{SYSTEM}\n{default_system}{im_end}'
    else:
        prompt = ''

    # Make sure we are completing the chat in the tone of the assistant
    if messages[-1].role != ASSISTANT:
        messages = messages + [Message(ASSISTANT, '')]

    for msg in messages:
        assert isinstance(msg.content, str)
        content = msg.content
        if allow_special:
            assert msg.role in (USER, ASSISTANT, SYSTEM, FUNCTION)
            if msg.function_call:
                assert msg.role == ASSISTANT
                tool_call = msg.function_call.arguments
                try:
                    tool_call = {'name': msg.function_call.name, 'arguments': json.loads(tool_call)}
                    tool_call = json.dumps(tool_call, ensure_ascii=False, indent=2)
                except json.decoder.JSONDecodeError:
                    tool_call = '{"name": "' + msg.function_call.name + '", "arguments": ' + tool_call + '}'
                if content:
                    content += '\n'
                content += f'<tool_call>\n{tool_call}\n</tool_call>'
        else:
            assert msg.role in (USER, ASSISTANT)
            assert msg.function_call is None
        if prompt:
            prompt += '\n'
        prompt += f'{im_start}{msg.role}\n{content}{im_end}'

    assert prompt.endswith(im_end)
    prompt = prompt[:-len(im_end)]
    return prompt


def encode_image_as_base64(path: str, max_short_side_length: int = -1) -> str:
    from PIL import Image
    image = Image.open(path)

    if (max_short_side_length > 0) and (min(image.size) > max_short_side_length):
        ori_size = image.size
        image = resize_image(image, short_side_length=max_short_side_length)
        logger.debug(f'Image "{path}" resized from {ori_size} to {image.size}.')

    image = image.convert(mode='RGB')
    buffered = BytesIO()
    image.save(buffered, format='JPEG')
    return 'data:image/jpeg;base64,' + base64.b64encode(buffered.getvalue()).decode('utf-8')


def encode_audio_as_base64(path: str) -> str:
    with open(path, 'rb') as audio_file:
        return 'data:;base64,' + base64.b64encode(audio_file.read()).decode('utf-8')


def encode_video_as_base64(path: str) -> str:
    with open(path, 'rb') as video_file:
        return 'data:;base64,' + base64.b64encode(video_file.read()).decode('utf-8')


def load_image_from_base64(image_base64: Union[bytes, str]):
    from PIL import Image
    image = Image.open(BytesIO(base64.b64decode(image_base64)))
    image.load()
    return image


def resize_image(img, short_side_length: int = 1080):
    from PIL import Image
    assert isinstance(img, Image.Image)

    width, height = img.size

    if width <= height:
        new_width = short_side_length
        new_height = int((short_side_length / width) * height)
    else:
        new_height = short_side_length
        new_width = int((short_side_length / height) * width)

    resized_img = img.resize((new_width, new_height), resample=Image.Resampling.BILINEAR)
    return resized_img


def get_last_usr_msg_idx(messages: List[Union[dict, Message]]) -> int:
    i = len(messages) - 1
    while (i >= 0) and (messages[i]['role'] != 'user'):
        i -= 1
    assert i >= 0, messages
    assert messages[i]['role'] == 'user'
    return i


def rm_default_system(messages: List[Message]) -> List[Message]:
    if len(messages) > 1 and messages[0].role == SYSTEM:
        if isinstance(messages[0].content, str):
            if messages[0].content.strip() == DEFAULT_SYSTEM_MESSAGE:
                return messages[1:]
            else:
                return messages
        elif isinstance(messages[0].content, list):
            if len(messages[0].content) == 1 and messages[0].content[0].text.strip() == DEFAULT_SYSTEM_MESSAGE:
                return messages[1:]
            else:
                return messages
        else:
            raise TypeError
    else:
        return messages


def get_message_stats(msg: Union[Message, dict, list, bool, None]) -> dict:
    """Return tokens and words for a message with consistency.
    Uses logic aligned with BaseChatModel._truncate_input_messages_roughly.
    
    BUG FIX: Handle unexpected list objects, boolean values, and None in messages list gracefully.
    When a raw list, bool, or None ends up in the conversation (instead of Message/dict),
    use defensive attribute access to prevent AttributeError.
    
    Args:
        msg: Can be a Message object, dict, list, bool, or None (for graceful handling).
        
    Returns:
        Dictionary with 'tokens' and 'words' counts. Returns zeros for unexpected types.
    """
    from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count
    from agent_cascade.log import logger
    
    # Handle None gracefully (defensive check)
    if msg is None:
        logger.debug("get_message_stats received None (skipping)")
        return {'tokens': 0, 'words': 0}
    
    if isinstance(msg, dict):
        if '_tokens' in msg and '_words' in msg:
            return {'tokens': msg['_tokens'], 'words': msg['_words']}
        role = msg.get(ROLE, '')
        function_call = msg.get('function_call')
        if role == ASSISTANT and function_call:
            text = f'{function_call}'
            stats = {'tokens': qwen_count(text), 'words': len(text.split())}
            msg['_tokens'] = stats['tokens']
            msg['_words'] = stats['words']
            return stats
        msg_obj = Message(**msg)
        is_dict = True
    elif isinstance(msg, list):
        # BUG FIX: Handle unexpected list objects gracefully
        # This can happen when streaming responses or multimodal content creates nested structures
        logger.debug(f"get_message_stats received a list instead of Message/dict (skipping): {str(msg)[:100]}")
        return {'tokens': 0, 'words': 0}
    elif isinstance(msg, bool):
        # BUG FIX: Handle unexpected boolean values gracefully
        # Booleans can leak into conversation history via JSON parsing or logger recovery
        logger.debug(f"get_message_stats received a bool instead of Message/dict (skipping): {msg}")
        return {'tokens': 0, 'words': 0}
    else:
        # Message object — use defensive attribute access
        role = getattr(msg, 'role', '')
        function_call = getattr(msg, 'function_call', None)
        if role == ASSISTANT and function_call:
            text = f'{function_call}'
            return {'tokens': qwen_count(text), 'words': len(text.split())}
        msg_obj = msg
        is_dict = False

    # Initialize LRU Cache for Message object stats (survives across get_message_stats calls)
    if not hasattr(get_message_stats, '_msg_stats'):
        get_message_stats._msg_stats = OrderedDict()
        get_message_stats._cache_max_size = 512
        
    msg_cache: OrderedDict = get_message_stats._msg_stats
    cache_max = get_message_stats._cache_max_size

    role = getattr(msg_obj, 'role', '')
    content = getattr(msg_obj, 'content', '')
    fc = getattr(msg_obj, 'function_call', None)
    
    # Build a hashable key from the message content using MD5 of the full text
    if fc:
        content_key = ('fc', str(fc))
    elif isinstance(content, list):
        text_items = [item.text for item in content if hasattr(item, 'text') and item.text]
        full_text = ''.join(text_items)
        content_hash = hashlib.md5(full_text.encode('utf-8', errors='replace')).hexdigest()[:16]
        content_key = ('multi', content_hash)
    else:
        full_text = str(content)
        content_hash = hashlib.md5(full_text.encode('utf-8', errors='replace')).hexdigest()[:16]
        content_key = ('text', content_hash)
    
    cache_key = (role, str(content_key))

    # Check Message object LRU cache — move to end on hit (most recently used)
    if cache_key in msg_cache:
        stats = msg_cache[cache_key]
        msg_cache.move_to_end(cache_key)
    else:
        text = extract_text_from_message(msg_obj, add_upload_info=True)
        image_tokens = 0

        def repl(match):
            nonlocal image_tokens
            image_tokens += IMAGE_TOKEN_ESTIMATE
            return f"[Image: {match.group(1)}]"
        
        text_for_tokens = IMAGE_REGEX.sub(repl, text)
        tokens = qwen_count(text_for_tokens) + image_tokens
        words = len(text.split())
        stats = {'tokens': tokens, 'words': words}
        
        # Evict oldest entry if at capacity
        if len(msg_cache) >= cache_max:
            msg_cache.popitem(last=False)
        msg_cache[cache_key] = stats

    if is_dict and isinstance(msg, dict):
        msg['_tokens'] = stats['tokens']
        msg['_words'] = stats['words']

    return stats


def get_history_stats(messages: List[Union[Message, dict, list, bool, None]]) -> dict:
    """Calculate total tokens and words in a message list with caching.
    
    Caching strategy:
    - Delegates caching and statistics logic to get_message_stats which has
      a built-in LRU cache for Message objects and inline cache mutation for dicts.
    """
    if not messages:
        return {'tokens': 0, 'words': 0}
    
    total_tokens = 0
    total_words = 0
    for m in messages:
        # Skip None, list, and bool values gracefully
        if m is None or isinstance(m, (list, bool)):
            continue
        stats = get_message_stats(m)
        total_tokens += stats['tokens']
        total_words += stats['words']
    return {'tokens': total_tokens, 'words': total_words}


def format_tool_result_preview(tool_name: str, content: str, max_len: int = 120) -> str:
    """Format a tool result message for display in activity banners.
    
    Returns strings like 'Tool read_file: Found 3 matches...' or 'Tool read_file completed'.
    """
    name = tool_name or 'tool'
    stripped = str(content).strip() if content else ''
    if stripped:
        return f"Tool {name}: {stripped[:max_len]}"
    return f"Tool {name} completed"
