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

import copy
import json
import os
import re
from typing import Dict, List, Literal, Union

import json5

from agent_cascade.llm.fncall_prompts.base_fncall_prompt import BaseFnCallPrompt
from agent_cascade.llm.schema import ASSISTANT, FUNCTION, SYSTEM, USER, ContentItem, FunctionCall, Message
from agent_cascade.log import logger
from agent_cascade.utils.utils import json_loads, repair_invalid_json

from agent_cascade.prompts.dna import XML_CONTENT_FIELDS, XML_MIN_LENGTH, FN_CALL_TEMPLATE, FN_CALL_TEMPLATE_WITH_CI


def _extract_xml_content_fields(text: str) -> Dict[str, str]:
    """Extract ANY XML-delimited content fields from tool call text.
    Matches <tag>content</tag> and returns a dict of tag -> content.
    """
    fields = {}
    # Find all tags that look like <tagname>content</tagname>
    # Supports any tag name composed of alphanumeric characters or underscores.
    pattern = r'<([a-zA-Z0-9_]+)>(.*?)</\1>'
    matches = re.finditer(pattern, text, re.DOTALL)
    for match in matches:
        tag = match.group(1)
        val = match.group(2)
        # Strip exactly one leading/trailing newline if present
        if val.startswith('\n'):
            val = val[1:]
        if val.endswith('\n'):
            val = val[:-1]
        fields[tag] = val
    return fields


def _strip_xml_content_fields(text: str) -> str:
    """Remove all XML tags and their content from text,
    leaving only the JSON portion."""
    # Matches any balanced <tag>...</tag>
    return re.sub(r'<([a-zA-Z0-9_]+)>.*?</\1>', '', text, flags=re.DOTALL).strip()


def _build_xml_tool_call(fn_name: str, arguments: dict) -> str:
    """Build a <tool_call> string, placing large content fields in XML tags
    outside the JSON to avoid escaping issues."""
    xml_parts = []
    json_args = {}

    if isinstance(arguments, dict):
        for k, v in arguments.items():
            if (k in XML_CONTENT_FIELDS
                    and isinstance(v, str)
                    and len(v) >= XML_MIN_LENGTH):
                xml_parts.append(f'<{k}>\n{v}\n</{k}>')
            else:
                json_args[k] = v
    else:
        # Fallback: arguments is already a string or something unexpected
        json_args = arguments

    fc = {'name': fn_name, 'arguments': json_args}
    fc_json = json.dumps(fc, ensure_ascii=False)
    if xml_parts:
        inner = fc_json + '\n' + '\n'.join(xml_parts)
    else:
        inner = fc_json
    return f'<tool_call>\n{inner}\n</tool_call>'


class NousFnCallPrompt(BaseFnCallPrompt):

    def preprocess_fncall_messages(self,
                                   messages: List[Message],
                                   functions: List[dict],
                                   lang: Literal['en', 'zh'],
                                   parallel_function_calls: bool = True,
                                   function_choice: Union[Literal['auto'], str] = 'auto',
                                   **kwargs) -> List[Message]:
        del lang  # ignored
        del parallel_function_calls  # ignored
        if function_choice != 'auto':
            raise NotImplementedError

        ori_messages = messages

        # Change function_call responses to plaintext responses:
        messages = []
        for msg in copy.deepcopy(ori_messages):
            role, content, reasoning_content = msg.role, msg.content, msg.reasoning_content
            if isinstance(content, str):
                content = [ContentItem(text=content)]
            else:
                content = (content or [])

            if role in (SYSTEM, USER):
                messages.append(Message(role=role, content=content, reasoning_content=reasoning_content))
            elif role == ASSISTANT:
                fn_call = msg.function_call
                if fn_call:
                    # Parse arguments from string to dict if needed
                    arguments = fn_call.arguments
                    try:
                        if isinstance(arguments, str) and arguments.strip():
                            if arguments.strip().startswith('```'):
                                arguments = re.sub(r'^```[a-zA-Z0-9]*\s*\n?', '', arguments.strip())
                                arguments = re.sub(r'\n?\s*```$', '', arguments)
                            if arguments.strip():
                                try:
                                    arguments = json_loads(arguments)
                                except Exception:
                                    arguments = arguments
                    except Exception:
                        logger.debug(f'Invalid json tool-calling arguments in history: {arguments}')

                    # Build the tool call text with XML-delimited content fields
                    if isinstance(arguments, dict):
                        fc = _build_xml_tool_call(fn_call.name, arguments)
                    else:
                        # Fallback: can't parse, emit as-is
                        fc_obj = {'name': fn_call.name, 'arguments': arguments}
                        fc = f'<tool_call>\n{json.dumps(fc_obj, ensure_ascii=False)}\n</tool_call>'

                    content.append(ContentItem(text=fc))
                if messages and messages[-1].role == ASSISTANT:
                    if messages[-1].content and messages[-1].content[-1].text and (
                            not messages[-1].content[-1].text.endswith('\n')):
                        messages[-1].content.append(ContentItem(text='\n'))
                    messages[-1].content.extend(content)
                else:
                    # TODO: Assuming there will only be one continuous reasoning_content here
                    messages.append(Message(role=role, content=content, reasoning_content=reasoning_content))
            elif role == FUNCTION:
                content = [ContentItem(text='<tool_response>\n')] + content + [ContentItem(text='\n</tool_response>')]
                if messages[-1].role == USER:
                    messages[-1].content.append(ContentItem(text='\n'))
                    messages[-1].content.extend(content)
                else:
                    messages.append(Message(role=USER, content=content))
            else:
                raise TypeError

        tool_descs = [{'type': 'function', 'function': f} for f in functions]
        tool_names = [function.get('name_for_model', function.get('name', '')) for function in functions]
        tool_descs = '\n'.join([json.dumps(f, ensure_ascii=False) for f in tool_descs])
        if SPECIAL_CODE_MODE and any([CODE_TOOL_PATTERN in x for x in tool_names]):
            tool_system = FN_CALL_TEMPLATE_WITH_CI.format(tool_descs=tool_descs)
        else:
            tool_system = FN_CALL_TEMPLATE.format(tool_descs=tool_descs)
        if messages and messages[0].role == SYSTEM:
            if isinstance(messages[0].content, str):
                messages[0].content = [ContentItem(text=messages[0].content)]
            messages[0].content.append(ContentItem(text='\n\n' + tool_system))
        else:
            messages = [Message(role=SYSTEM, content=[ContentItem(text=tool_system)])] + messages
        return messages
    
    def postprocess_fncall_messages(
        self,
        messages: List[Message],
        parallel_function_calls: bool = True,
        function_choice: Union[Literal['auto'], str] = 'auto',
        thought_in_content: bool = False,
    ) -> List[Message]:
        if function_choice != 'auto':
            raise NotImplementedError
        # Convert plaintext responses to function_call responses:
        new_messages = []
        tool_id = 1
        for msg in messages:
            role, content, reasoning_content, extra = msg.role, msg.content, msg.reasoning_content, msg.extra
            extra = extra or {}
            assert isinstance(content, list)

            if role in (SYSTEM, USER):
                new_messages.append(
                    Message(role=role, content=content, reasoning_content=reasoning_content, extra=extra))
                continue

            # Reasoning content is placed in a separate message
            if reasoning_content:
                new_messages.append(Message(role=role, content='', reasoning_content=reasoning_content, extra=extra))

            new_content = []
            for item in content:
                item_type, item_text = item.get_type_and_value()

                if item_type != 'text':  # multimodal
                    new_content.append(item)
                    continue
                # Do not parse <tool_call> in thought!!!
                if '<think>' in item_text:
                    thought_in_content = True
                if thought_in_content:
                    if '</think>' not in item_text:
                        new_content.append(ContentItem(text=item_text))
                        continue
                    _item_text = item_text.split('</think>')
                    # assert len(_item_text) == 2
                    new_content.append(ContentItem(text='</think>'.join(_item_text[:-1]) + '</think>'))
                    item_text = _item_text[-1]

                # Normalize Gemma format: <|tool_call>call:fn_name{args}<tool_call|>
                def gemma_repl(match):
                    fn_name = match.group(1)
                    args_str = match.group(2)
                    if not args_str.strip():
                        args_str = "{}"
                    return f'<tool_call>\n{{"name": "{fn_name}", "arguments": {args_str}}}\n</tool_call>'
                # Support both <tool_call> and <|tool_call|> (Gemma/Qwen style)
                text = item_text
                text = text.replace('<|tool_call|>', '<tool_call>')
                text = text.replace('</|tool_call|>', '</tool_call>')
                text = text.replace('<tool_call|>', '</tool_call>')

                if '<tool_call>' not in text:
                    # Fallback: if model uses our XML tags but forgot the <tool_call> wrapper
                    # Check for common indicators: <name>, <tool_name>, <arguments>, or call:name{
                    indicators = ['<name>', '<tool_name>', '<arguments>', 'call:']
                    tag_pos = -1
                    for ind in indicators:
                        p = text.find(ind)
                        if p != -1 and (tag_pos == -1 or p < tag_pos):
                            tag_pos = p
                    
                    if tag_pos != -1:
                        # Auto-wrap the rest of the text as a tool call
                        text = text[:tag_pos] + '<tool_call>' + text[tag_pos:] + '</tool_call>'
                        logger.info("Auto-wrapped unwrapped tool call in model output.")

                i = text.find('<tool_call>')
                # If no function call:
                if i < 0:
                    show_text = text
                    if show_text:
                        new_content.append(ContentItem(text=show_text))
                    continue

                # split tool-call to separate assistant msg
                tool_call_list = text.split('<tool_call>')
                pre_thought = tool_call_list[0]
                if pre_thought.strip():
                    new_content.append(ContentItem(text=pre_thought))
                for txt in tool_call_list[1:]:
                    if not txt.strip():
                        continue

                    if '</tool_call>' not in txt:
                        # incomplete </tool_call>: This is to better represent incomplete tool calls in streaming output
                        fn_name, fn_args = extract_fn(txt)
                        if fn_name:  # need to call function
                            if new_content:
                                new_messages.append(Message(
                                    role=role,
                                    content=new_content,
                                    extra=extra,
                                ))  # split thought and function call
                                new_content = []
                            # TODO: process incomplete tool-call messages
                            _extra = copy.deepcopy(extra) if extra else {'function_id': ''}
                            _extra['function_id'] = str(tool_id)
                            tool_id += 1
                            new_messages.append(
                                Message(
                                    role=ASSISTANT,
                                    content=[],
                                    function_call=FunctionCall(
                                        name=fn_name,
                                        arguments=fn_args,
                                    ),
                                    extra=_extra,
                                ))
                        continue

                    one_tool_call_txt = txt.split('</tool_call>')

                    # The complete tool-call response
                    if new_content:
                        new_messages.append(Message(
                            role=role,
                            content=new_content,
                            extra=extra,
                        ))  # split thought and function call
                        new_content = []
                    fn = None
                    raw_tool_text = one_tool_call_txt[0]

                    # --- Phase 2: Extract XML content fields first ---
                    xml_fields = _extract_xml_content_fields(raw_tool_text)
                    # Strip XML fields to leave only the JSON portion
                    json_portion = _strip_xml_content_fields(raw_tool_text).strip()

                    # Legacy SPECIAL_CODE_MODE: handle <code> inside tool calls
                    if SPECIAL_CODE_MODE and '<code>' in raw_tool_text and '</code>' in raw_tool_text:
                        _snips = raw_tool_text.split('<code>')
                        for i, _s in enumerate(_snips):
                            if i == 0:
                                try:
                                    content_to_parse = _s.strip()
                                    if content_to_parse.startswith('```'):
                                        content_to_parse = re.sub(r'^```[a-zA-Z0-9]*\s*\n?', '', content_to_parse)
                                        content_to_parse = re.sub(r'\n?\s*```$', '', content_to_parse)
                                    fn = json5.loads(content_to_parse)
                                except Exception:
                                    fn = {'name': 'code_interpreter', 'arguments': {}}
                            else:
                                code = _s.replace('</code>', '')
                                if fn and 'arguments' in fn:
                                    fn['arguments']['code'] = code
                    else:
                        # Try parsing the JSON portion (with XML fields stripped)
                        try:
                            if json_portion.startswith('```'):
                                json_portion = re.sub(r'^```[a-zA-Z0-9]*\s*\n?', '', json_portion)
                                json_portion = re.sub(r'\n?\s*```$', '', json_portion)
                            fn = json_loads(json_portion)
                        except Exception:
                            fn = None

                    # --- Merge XML fields into parsed arguments ---
                    if xml_fields:
                        if not fn:
                            # If JSON was missing/invalid, try to boot from XML name
                            if 'name' in xml_fields:
                                fn = {'name': xml_fields.pop('name'), 'arguments': {}}
                            else:
                                # Last ditch: regex search for name in text
                                fn_name_match = re.search(r'["\']?name["\']?\s*:\s*["\']([^"\']+)["\']', raw_tool_text)
                                if fn_name_match:
                                    fn = {'name': fn_name_match.group(1), 'arguments': {}}
                        
                        if fn:
                            # 1. Promote flat JSON to {name, arguments} structure if needed
                            if 'name' not in fn and 'arguments' not in fn:
                                # It's a flat arguments dict
                                fn = {'name': '', 'arguments': fn}
                            elif 'name' in fn and 'arguments' not in fn:
                                # It's a dict with name but flat arguments
                                _name = fn.pop('name')
                                fn = {'name': _name, 'arguments': fn}
                            
                            # 2. XML name overrides JSON name if both present
                            if 'name' in xml_fields:
                                fn['name'] = xml_fields.pop('name')
                            
                            # 3. Ensure arguments is a dict
                            if 'arguments' not in fn:
                                fn['arguments'] = {}
                            if isinstance(fn['arguments'], str):
                                try:
                                    fn['arguments'] = json_loads(fn['arguments'])
                                except Exception:
                                    fn['arguments'] = {}
                            
                            # 4. Merge remaining XML fields into arguments
                            fn['arguments'].update(xml_fields)

                    if fn and 'name' in fn and 'arguments' in fn:
                        _extra = copy.deepcopy(extra) if extra else {}
                        _extra['function_id'] = str(tool_id)
                        tool_id += 1
                        new_messages.append(
                            Message(
                                role=ASSISTANT,
                                content=[],
                                function_call=FunctionCall(
                                    name=fn['name'],
                                    arguments=json.dumps(fn['arguments'], ensure_ascii=False),
                                ),
                                extra=_extra,
                            ))

            if new_content:
                new_messages.append(Message(role=role, content=new_content, extra=extra))
        return new_messages


# Templates are now imported from agent_cascade.prompts.dna

SPECIAL_CODE_MODE = os.getenv('SPECIAL_CODE_MODE', 'false').lower() == 'true'
CODE_TOOL_PATTERN = 'code_interpreter'
# Template with CI is now imported from agent_cascade.prompts.dna


# Mainly for removing incomplete special tokens when streaming the output
# This assumes that '<tool_call>\n{"name": "' is the special token for the NousFnCallPrompt
def remove_incomplete_special_tokens(text: str) -> str:
    if text in '<tool_call>\n{"name": "':
        text = ''
    return text


def extract_fn(text: str):
    """Fallback extraction when standard JSON parsing fails."""
    fn_name, fn_args = '', ''
    # Match "name": "..." or name: "..." or 'name': "..."
    fn_name_match = re.search(r'["\']?name["\']?\s*:\s*["\']([^"\']+)["\']', text)
    if fn_name_match:
        fn_name = fn_name_match.group(1)
        
    # Match "arguments": { ... } or arguments: { ... }
    fn_args_match = re.search(r'["\']?arguments["\']?\s*:\s*(\{.*\})', text, re.DOTALL)
    if fn_args_match:
        fn_args = fn_args_match.group(1)
    else:
        # Fallback to older string slicing if regex fails to find the full block
        fn_args_s = '"arguments": '
        k = text.find(fn_args_s)
        if k > 0:
            fn_args = text[k + len(fn_args_s):].strip()
            if fn_args.endswith('}'):
                pass # keep it
            elif fn_args.count('{') > fn_args.count('}'):
                fn_args += '}' # simple repair
                
    return fn_name, fn_args
