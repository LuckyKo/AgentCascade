"""
Quick WebUI Demo with LM Studio
Make sure LM Studio is running with a model loaded.

This example shows:
1. System prompt (instructions for the assistant)
2. Custom tools (functions the assistant can call)
"""

import json
import os
import requests
from bs4 import BeautifulSoup
from agent_cascade.agents import Assistant
from agent_cascade.gui import WebUI
from agent_cascade.tools.base import BaseTool, register_tool

# Step 1: Create custom tools

@register_tool('get_weather', allow_overwrite=True)
class GetWeather(BaseTool):
    description = 'Get the current weather for a given city'
    parameters = [{
        'name': 'city',
        'type': 'string',
        'description': 'The name of the city',
        'required': True
    }]

    def call(self, params: str, **kwargs) -> str:
        params = json.loads(params)
        city = params['city']
        
        # Dummy weather data (replace with real API call)
        weather_data = {
            'london': '15°C, Cloudy',
            'new york': '22°C, Sunny',
            'tokyo': '18°C, Rainy',
            'paris': '20°C, Partly Cloudy',
        }
        
        weather = weather_data.get(city.lower(), '20°C, Clear')
        return json.dumps({'city': city, 'weather': weather})


@register_tool('web_search', allow_overwrite=True)
class WebSearch(BaseTool):
    description = 'Search for information from the internet. Use this when you need to find current events, facts, or recent information.'
    parameters = [{
        'name': 'query',
        'type': 'string',
        'description': 'The search query',
        'required': True
    }]

    def call(self, params: str, **kwargs) -> str:
        params = json.loads(params)
        query = params['query']
        
        # Using DuckDuckGo HTML search (no API key needed)
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            url = f'https://html.duckduckgo.com/html/?q={requests.utils.quote(query)}'
            response = requests.get(url, headers=headers, timeout=10)
            soup = BeautifulSoup(response.text, 'html.parser')
            
            results = []
            for result in soup.select('.result')[:5]:  # Get top 5 results
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


@register_tool('visit_website', allow_overwrite=True)
class VisitWebsite(BaseTool):
    description = 'Visit a website and extract its content. Use this when you need to read the full content of a specific webpage.'
    parameters = [{
        'name': 'url',
        'type': 'string',
        'description': 'The URL of the website to visit',
        'required': True
    }]

    def call(self, params: str, **kwargs) -> str:
        params = json.loads(params)
        url = params['url']
        
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Remove script and style elements
            for script in soup(['script', 'style', 'nav', 'footer', 'header']):
                script.decompose()
            
            # Get main content
            title = soup.title.string if soup.title else 'No title'
            main_content = soup.get_text(separator='\n', strip=True)
            
            # Truncate if too long
            max_length = 3000
            if len(main_content) > max_length:
                main_content = main_content[:max_length] + '\n\n[Content truncated...]'
            
            return f'Title: {title}\n\nContent:\n{main_content}'
        except Exception as e:
            return f'Failed to visit website: {str(e)}'


# Step 2: Configure LM Studio
llm_cfg = {
    'model': 'your-model-name',
    'model_server': 'http://localhost:1234/v1',
    'api_key': 'EMPTY',
}

# Step 3: Define system prompt
system_instruction = '''You are a helpful assistant with access to multiple tools:

1. **get_weather**: Get current weather for a city
2. **web_search**: Search the internet for information (uses DuckDuckGo)
3. **visit_website**: Visit a specific URL and extract its content

When users ask about:
- Weather: Use get_weather tool
- Current events, facts, or general information: Use web_search first
- A specific website URL: Use visit_website to read its content

Always be friendly, concise, and cite your sources when using search results.'''

# Step 4: Create assistant with system prompt and tools
tools = ['get_weather', 'web_search', 'visit_website']
bot = Assistant(
    llm=llm_cfg,
    system_message=system_instruction,
    function_list=tools
)

# Step 5: Configure UI
chatbot_config = {
    'input.placeholder': 'Ask me anything! Try weather, search, or website visits...',
    'prompt.suggestions': [
        "What's the weather in London?",
        "Search for latest AI news",
        "Visit https://example.com and tell me what's on it",
        "Who won the latest football match?",
    ],
    'user.name': 'You',
}

# Launch WebUI
WebUI(bot, chatbot_config=chatbot_config).run()
