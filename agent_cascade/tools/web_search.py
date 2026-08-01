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

Backend priority is configurable via config/secrets.json:
  - "search_backend_priority": ["serper", "duckduckgo"]  (default order)

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

_KNOWN_BACKENDS = {"serper", "duckduckgo"}
_DEFAULT_PRIORITY = ["serper", "duckduckgo"]


def get_search_backend_priority() -> List[str]:
    """Get configured search backend priority from secrets.json.

    Returns a list of backend names in priority order, filtered to known backends.
    Falls back to default ["serper", "duckduckgo"] if missing or invalid.
    """
    value = get_secret("search_backend_priority")
    if not isinstance(value, list):
        return list(_DEFAULT_PRIORITY)

    filtered = [b for b in value if isinstance(b, str) and b in _KNOWN_BACKENDS]
    return filtered if filtered else list(_DEFAULT_PRIORITY)


def _resolve_serper_api_key() -> str:
    """Resolve Serper API key from secrets config or environment variable."""
    # Prefer dedicated secrets config if present
    key = get_secret("serper_api_key")
    if isinstance(key, str) and key.strip():
        return key.strip()
    # Fallback to environment variable
    return os.getenv('SERPER_API_KEY', '').strip()


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
        """Execute a web search using configured backend priority.

        Backend selection logic:
          - Uses "search_backend_priority" from config/secrets.json (default: ["serper", "duckduckgo"]).
          - Iterates through backends in order until one succeeds.
          - Serper requires an API key; skipped if unavailable.

        Args:
            params: JSON string or dict containing 'query'.
            **kwargs: Ignored.

        Returns:
            Formatted search results string.

        Raises:
            RuntimeError: If all configured backends fail or are unavailable.
        """
        params = self._verify_json_format_args(params)
        query = params['query']

        priority = get_search_backend_priority()
        last_error = None

        for backend in priority:
            try:
                if backend == "serper":
                    api_key = _resolve_serper_api_key()
                    if not api_key:
                        logger.info(f"Serper in priority but no API key configured, skipping")
                        continue
                    search_results = self._search_serper(query, api_key)
                    return self._format_serper_results(search_results)

                elif backend == "duckduckgo":
                    return search_duckduckgo(query)

            except (requests.RequestException, ValueError, RuntimeError) as e:
                last_error = e
                logger.info(f"{backend.capitalize()} failed ({e}), trying next backend")

        raise RuntimeError(
            f"Web search failed: all configured backends unavailable ({', '.join(priority)}). "
            f"Last error: {last_error}"
        ) from last_error

    @staticmethod
    def _search_serper(query: str, api_key: str) -> List[Any]:
        """Perform search via Serper API.

        Raises on any failure so caller can fall back to DDG.
        """
        headers = {'Content-Type': 'application/json', 'X-API-KEY': api_key}
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