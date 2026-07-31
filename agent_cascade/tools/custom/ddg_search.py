"""DuckDuckGo web search backend — internal helper for web_search tool.

This module is internal and used by web_search as its fallback backend. It is NOT
exposed directly to agents via public exports. New code should use web_search, which
internally calls search_duckduckgo when Serper is unavailable or fails.

The DDGSearch class is kept only for backwards compatibility with existing imports
and should not be used directly. Prefer the web_search tool instead.
"""

from typing import List

import requests
from bs4 import BeautifulSoup

from agent_cascade.tools.base import BaseTool


def search_duckduckgo(query: str) -> str:
    """Internal helper: perform a DuckDuckGo HTML-based web search.

    Returns formatted results string suitable for direct use as tool output.

    Args:
        query: The search query string.

    Returns:
        Formatted search results or an error message.
    """
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        url = f'https://html.duckduckgo.com/html/?q={requests.utils.quote(query)}'
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        results: List[str] = []
        for result in soup.select('.result')[:5]:
            title_elem = result.select_one('.result__title')
            snippet_elem = result.select_one('.result__snippet')
            url_elem = result.select_one('.result__url')
            if title_elem and snippet_elem:
                title = title_elem.get_text(strip=True)
                snippet = snippet_elem.get_text(strip=True)
                url_text = url_elem.get_text(strip=True) if url_elem else ''
                results.append(f'Title: {title}\nSnippet: {snippet}\nURL: {url_text}')
        if results:
            return '\n\n'.join(results)
        return 'No results found.'
    except requests.RequestException as e:
        raise RuntimeError(f"DuckDuckGo search failed: {e}") from e


class DDGSearch(BaseTool):
    """DEPRECATED: Internal-only DuckDuckGo search tool.

    Kept for backwards compatibility with existing imports. Agents should use
    web_search instead, which transparently selects the best available backend.
    """

    name = 'ddg_search'
    description = 'Search the internet via DuckDuckGo (internal fallback backend).'
    parameters = {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': 'The search query'
            }
        },
        'required': ['query'],
    }

    def call(self, params: str, **kwargs) -> str:
        params = self._verify_json_format_args(params)
        return search_duckduckgo(params['query'])