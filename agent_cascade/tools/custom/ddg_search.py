"""DuckDuckGo web search tool — shared module to avoid duplication between entry points."""

import requests
from bs4 import BeautifulSoup

from agent_cascade.tools.base import BaseTool


class DDGSearch(BaseTool):
    """Search the internet via DuckDuckGo's HTML interface (no API key required)."""

    name = 'ddg_search'
    description = 'Search for information from the internet using DuckDuckGo (No API key required).'
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
        query = params['query']
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            url = f'https://html.duckduckgo.com/html/?q={requests.utils.quote(query)}'
            response = requests.get(url, headers=headers, timeout=10)
            soup = BeautifulSoup(response.text, 'html.parser')
            results = []
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
        except Exception as e:
            return f'Search failed: {str(e)}'