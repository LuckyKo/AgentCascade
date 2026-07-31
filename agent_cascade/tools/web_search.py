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

"""Unified web search tool with automatic backend selection.

Backend priority:
  1. Serper (when SERPER_API_KEY is configured via config/secrets.json or env var)
  2. DuckDuckGo (fallback when no key or Serper call fails)

Configuration:
  - Preferred: set "serper_api_key" in config/secrets.json (gitignored).
  - Fallback: set SERPER_API_KEY environment variable.
"""

import os
from typing import Any, List, Union

import requests

from agent_cascade.log import logger
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.tools.custom.ddg_search import search_duckduckgo
from config.secrets_loader import get_secret


def _resolve_serper_api_key() -> str:
    """Resolve Serper API key from secrets config or environment variable."""
    # Prefer dedicated secrets config if present
    key = get_secret("serper_api_key")
    if isinstance(key, str) and key.strip():
        return key.strip()
    # Fallback to environment variable
    return os.getenv('SERPER_API_KEY', '').strip()


SERPER_API_KEY = _resolve_serper_api_key()
SERPER_URL = os.getenv('SERPER_URL', 'https://google.serper.dev/search')


@register_tool('web_search', allow_overwrite=True)
class WebSearch(BaseTool):
    """Unified web search tool.

    Automatically selects the best available backend:
    - Serper (primary, when SERPER_API_KEY is configured)
    - DuckDuckGo (fallback)
    """

    name = 'web_search'
    description = TOOL_METADATA['web_search']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': TOOL_METADATA['web_search']['parameters']['query']
            }
        },
        'required': ['query'],
    }

    def call(self, params: Union[str, dict], **kwargs) -> str:
        """Execute a web search using the best available backend.

        Backend selection logic:
          1. Serper API key is resolved from config/secrets.json ("serper_api_key") or SERPER_API_KEY env var.
          2. If key is present → use Serper.
          3. If Serper call fails (network error, auth error, etc.) → fall back to DuckDuckGo.
          4. If no key configured → use DuckDuckGo directly.

        Args:
            params: JSON string or dict containing 'query'.
            **kwargs: Ignored.

        Returns:
            Formatted search results string.
        """
        params = self._verify_json_format_args(params)
        query = params['query']

        # Try Serper first if configured
        if SERPER_API_KEY:
            try:
                search_results = self._search_serper(query)
                return self._format_serper_results(search_results)
            except Exception as e:
                # Log fallback reason for debugging; fall through to DDG
                logger.info("Serper failed (%s), falling back to DuckDuckGo", e)

        # Fallback to DuckDuckGo (or primary if no Serper key)
        return search_duckduckgo(query)

    @staticmethod
    def _search_serper(query: str) -> List[Any]:
        """Perform search via Serper API.

        Raises on any failure so caller can fall back to DDG.
        """
        headers = {'Content-Type': 'application/json', 'X-API-KEY': SERPER_API_KEY}
        payload = {'q': query}
        response = requests.post(SERPER_URL, json=payload, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        organic = data.get('organic')
        if not organic:
            raise ValueError("Serper returned no organic results")
        return organic

    @staticmethod
    def _format_serper_results(search_results: List[Any]) -> str:
        """Format Serper results into a readable string."""
        content = '```\n{}\n```'.format('\n\n'.join([
            f"[{i}]\"{doc['title']}\n{doc.get('snippet', '')}\"{doc.get('date', '')}"
            for i, doc in enumerate(search_results, 1)
        ]))
        return content