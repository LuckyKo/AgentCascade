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

from typing import Optional, Union

from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.tools.simple_doc_parser import SimpleDocParser


@register_tool('web_extractor')
class WebExtractor(BaseTool):
    description = TOOL_METADATA['web_extractor']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'url': {
                'description': TOOL_METADATA['web_extractor']['parameters']['url'],
                'type': 'string',
            },
            'extract_images': {
                'description': TOOL_METADATA['web_extractor']['parameters']['extract_images'],
                'type': 'boolean',
                'default': True,
            }
        },
        'required': ['url'],
    }

    def __init__(self, cfg: Optional[dict] = None):
        super().__init__(cfg)
        self.work_dir: str = self.cfg.get('work_dir', '')

    def call(self, params: Union[str, dict], **kwargs) -> str:
        params = self._verify_json_format_args(params)
        url = params['url']
        # extract_images is a per-call param (default True). Build a fresh parser per call so
        # concurrent calls on the same WebExtractor instance can't clobber each other's flag.
        parser = SimpleDocParser(cfg={'work_dir': self.work_dir,
                                      'extract_image': bool(params.get('extract_images', True))})
        try:
            parsed_web = parser.call({'url': url})
            return parsed_web
        except Exception as e:
            # Translate failures into a clean, actionable message — no traceback or internal
            # file paths leak to the calling agent. Preserve the URL (so it can be corrected)
            # and any HTTP status code for quick diagnosis.
            detail = self._describe_fetch_error(e)
            return (f"Failed to fetch {url}: {detail}. "
                    'Check that the URL is correct, the page exists, and is not blocked or '
                    'requiring JavaScript rendering.')

    @staticmethod
    def _describe_fetch_error(e: Exception) -> str:
        """Reduce an exception to a short human/agent-readable reason (no traceback)."""
        msg = str(e)
        # Extract an HTTP status code if present (e.g. "404 Client Error: Not Found ...").
        import re
        m = re.search(r'\b([45]\d{2})\b', msg)
        if m:
            code = m.group(1)
            reason_map = {'404': 'HTTP 404 (Not Found)', '403': 'HTTP 403 (Forbidden)',
                          '410': 'HTTP 410 (Gone)', '429': 'HTTP 429 (Too Many Requests)'}
            return reason_map.get(code, f'HTTP {code}')
        # Connection-level failures.
        low = msg.lower()
        if 'timed out' in low or 'timeout' in low:
            return 'connection timed out'
        if 'name resolution' in low or 'getaddrinfo' in low or 'failed to establish' in low:
            return 'DNS / network error (could not reach the host)'
        if 'certificate' in low or 'ssl' in low:
            return 'SSL/TLS certificate error'
        # Fallback: first line of the message, truncated — still no traceback.
        first = msg.splitlines()[0] if msg else e.__class__.__name__
        return first[:160]
