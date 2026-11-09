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

import os
import re
from typing import List, Optional, Tuple

from agent_cascade.llm import ModelServiceError
from agent_cascade.llm.base import register_llm
from agent_cascade.llm.oai import TextChatAtOAI
from agent_cascade.llm.schema import ContentItem, Message
from agent_cascade.log import logger
from agent_cascade.settings import IMAGE_MAX_SHORT_SIDE
from agent_cascade.utils.utils import (_DEFAULT_VIDEO_MIME, encode_audio_as_base64, encode_image_as_base64,
                                       encode_video_as_base64, sanitize_chrome_file_path)

# Matches a base64 data-URI: ``data:<mime>;base64,<b64>``, where <mime> may be
# empty (the untagged ``data:;base64,...`` produced by the inline MediaStorageError
# fallback in content_parse). group(1)=mime (possibly ''), group(2)=pure base64.
_DATA_URI_RE = re.compile(r'data:([^;]*);base64,(.+)', re.IGNORECASE)


@register_llm('qwenvl_oai')
class QwenVLChatAtOAI(TextChatAtOAI):

    @property
    def support_multimodal_input(self) -> bool:
        return True

    def _effective_generate_cfg(self, generate_cfg: Optional[dict] = None) -> dict:
        """The cfg that actually governs THIS call.

        At runtime the endpoint's modality flags ride in the *merged* generate_cfg
        (``_build_merged_cfg`` → ``llm.chat(extra_generate_cfg=...)``), NOT in the
        template ``self.generate_cfg``. ``convert_messages_to_dicts`` is invoked from
        ``oai`` with that merged dict, so thread it here to mirror ``effective_vision``
        in ``llm/base.py``. When absent (direct construction / unit tests), fall back
        to ``self.generate_cfg``.
        """
        return self.generate_cfg if generate_cfg is None else generate_cfg

    def _audio_input_enabled(self, generate_cfg: Optional[dict] = None) -> bool:
        """Whether to emit ``input_audio`` parts for audio ContentItems.

        Compose the class capability OR the per-endpoint ``audio_enabled`` cfg
        flag. ``APIEndpoint.to_llm_cfg`` emits ``audio_enabled`` on EVERY call
        (it is a dataclass field defaulting to False), so the key is always
        present at runtime. A plain ``.get('audio_enabled', self.support_audio_input)``
        would therefore let a present-False win over a subclass that overrides
        ``support_audio_input`` to True (``QwenOmniChatAtOAI``), silently
        degrading its audio to a text path-reference. OR-ing keeps a capability-
        declaring subclass on while preserving the default-off invariant: an
        image-only adapter (``support_audio_input`` False) with the flag absent
        or False yields ``False or False`` -> False -> text fallback (today's
        behavior unchanged).
        """
        return self.support_audio_input or self._effective_generate_cfg(
            generate_cfg).get('audio_enabled', False)

    def _video_input_enabled(self, generate_cfg: Optional[dict] = None) -> bool:
        """Whether to emit ``video_url`` / ``video`` parts for video ContentItems.

        Driven purely by the endpoint cfg ``video_enabled`` flag (default False),
        since this image-only adapter has no class-level video-input capability
        to fall back to.
        """
        return self._effective_generate_cfg(generate_cfg).get('video_enabled', False)

    @staticmethod
    def _audio_format_from_path(path) -> Optional[str]:
        """Derive the ``input_audio`` 'format' from a local audio path's extension.

        Returns the extension lowercased with no dot (``.wav`` -> ``wav``), verbatim
        — NOT restricted to OpenAI's ``wav|mp3`` whitelist; llama.cpp omni servers
        accept the file's own container format. A local path with no extension
        returns None, so the part keeps the no-'format' shape rather than asserting
        an unverified ``wav``. A non-local ``http``/``data`` URL is left unchanged by
        ``conv_multimodel_value`` and returns None here, so the part keeps today's
        no-'format' shape.
        """
        if not isinstance(path, str) or path.startswith(('http://', 'https://', 'data:')):
            return None
        ext = os.path.splitext(path)[1].lower().lstrip('.')
        return ext or None

    @staticmethod
    def _split_audio_data_uri(data_uri: str) -> Tuple[str, Optional[str]]:
        """Split an inline audio data-URI into ``(pure_base64, format_or_None)``.

        ``content_parse``'s ``MediaStorageError`` fallback hands the converter a raw
        ``data:<mime>;base64,<b64>`` string instead of a local path, but the
        ``input_audio`` part expects PURE base64 in ``data`` plus a ``format`` —
        the backend-verbatim container format, same as the locally-encoded path
        produces, NOT restricted to OpenAI's ``wav|mp3`` whitelist (llama.cpp omni
        servers accept the container format). Strip the
        prefix here and derive ``format`` from the MIME subtype (``audio/wav`` ->
        ``wav``). An untagged ``data:;base64,...`` carries no MIME, so no format
        is emitted. A string that isn't a base64 data-URI is returned unchanged
        with no format (best-effort; shouldn't happen for base64 media).
        """
        m = _DATA_URI_RE.match(data_uri)
        if not m:
            return data_uri, None
        mime = m.group(1)
        fmt = mime.rsplit('/', 1)[-1].lower() if '/' in mime else None
        return m.group(2), fmt or None

    @staticmethod
    def _tag_video_data_uri(url: str) -> str:
        """Normalize an untagged video data-URI for the ``video_url`` part.

        ``video_url`` requires a MIME-tagged data-URI: a local path is already
        encoded as ``data:video/<mime>;base64,...`` by ``encode_video_as_base64``
        and an ``http``/``https`` URL is passed through untouched, so only the
        inline fallback's untagged ``data:;base64,...`` needs a ``video/mp4``
        default injected. A URI that already carries a MIME subtype is kept
        verbatim.
        """
        if url.startswith('data:;base64,'):
            return f'data:{_DEFAULT_VIDEO_MIME};base64,' + url[len('data:;base64,'):]
        return url

    def convert_messages_to_dicts(self,
                                  messages: List[Message],
                                  generate_cfg: Optional[dict] = None) -> List[dict]:
        new_messages = []

        for msg in messages:
            content = msg.content
            if isinstance(content, str):
                content = [ContentItem(text=content)]
            assert isinstance(content, list)

            new_content = []
            for item in content:
                t, v = item.get_type_and_value()
                if t == 'text' and v:
                    new_content.append({'type': 'text', 'text': v})
                if t in ['image', 'video', 'audio']:
                    # Only translate a modality into a multimodal content part if THIS
                    # endpoint actually supports it. This adapter is image-only by
                    # default; audio/video are emitted only when the endpoint cfg sets
                    # audio_enabled / video_enabled (the omni case, e.g. LFM3-Omni).
                    # Otherwise keep the media as a TEXT path-reference so the agent
                    # still sees the file the tool produced (and an image-only server
                    # never receives an input_audio/video_url it would reject + loop on).
                    if t == 'audio' and not self._audio_input_enabled(generate_cfg):
                        new_content.append({'type': 'text', 'text': f'[Audio file: {v}]'})
                        continue
                    if t == 'video' and not self._video_input_enabled(generate_cfg):
                        new_content.append({'type': 'text', 'text': f'[Video file: {v}]'})
                        continue
                    try:
                        orig_v = v  # pre-conv snapshot: set for all media, consumed only in the audio branch's 'format' (conv_* rewrites v)
                        if isinstance(v, str):
                            v = conv_multimodel_value(t, v)
                        if isinstance(v, list):
                            new_v = []
                            for _v in v:
                                new_v.append(conv_multimodel_value(t, _v))
                            v = new_v
                        if isinstance(v, dict):
                            v['data'] = conv_multimodel_value(t, v['data'])

                        if t == 'image':
                            new_content.append({'type': 'image_url', 'image_url': {'url': v}})
                        elif t == 'video':
                            if isinstance(v, str):
                                new_content.append({
                                    'type': 'video_url',
                                    'video_url': {'url': self._tag_video_data_uri(v)}
                                })
                            elif isinstance(v, list):
                                new_content.append({'type': 'video', 'video': v})
                            else:
                                raise TypeError
                        elif t == 'audio':
                            if isinstance(v, str):
                                # An inline data-URI (the content_parse MediaStorageError
                                # fallback) still carries the 'data:<mime>;base64,' prefix;
                                # the input_audio part wants pure base64 + a 'format', so
                                # split it here. A locally-encoded path arrives as pure
                                # base64 already and keeps its extension-derived format.
                                if v.startswith('data:'):
                                    data, fmt = self._split_audio_data_uri(v)
                                else:
                                    data, fmt = v, self._audio_format_from_path(orig_v)
                                part = {'data': data}
                                if fmt:
                                    part['format'] = fmt
                                new_content.append({'type': 'input_audio', 'input_audio': part})
                            elif isinstance(v, dict):
                                new_content.append({'type': 'input_audio', 'input_audio': v})
                            else:
                                raise TypeError
                    except (ModelServiceError, TypeError) as e:
                        err_msg = str(e)
                        logger.warning(f'Multimodal item error: {err_msg}')
                        # Provide feedback to the agent about the missing file
                        new_content.append({
                            'type':
                                'text',
                            'text':
                                f'\n[System Error: {err_msg} - This multimodal item was skipped and is invisible to the LLM]\n'
                        })
                        continue

            new_msg = msg.model_dump()
            new_msg['content'] = new_content
            new_messages.append(new_msg)
        new_messages = self._conv_agent_cascade_messages_to_oai(new_messages)

        return new_messages


def conv_multimodel_value(t, v):
    if v.startswith('file://'):
        v = sanitize_chrome_file_path(v)
    if not v.startswith(('http://', 'https://', 'data:')):
        v_exists = os.path.exists(v)
        # On Windows, absolute paths like /N:/path are sometimes passed.
        # Try stripping leading slash if it doesn't exist.
        if not v_exists and os.name == 'nt' and v.startswith('/') and len(v) > 2 and v[2] == ':':
            v = v[1:]
            v_exists = os.path.exists(v)

        if v_exists:
            if t == 'image':
                v = encode_image_as_base64(v, max_short_side_length=IMAGE_MAX_SHORT_SIDE)
            elif t == 'video':
                v = encode_video_as_base64(v)
            elif t == 'audio':
                v = encode_audio_as_base64(v)
            else:
                raise TypeError
        else:
            raise ModelServiceError(f'Local file "{v}" does not exist.')
    return v
