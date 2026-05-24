import requests
import json

url = "http://localhost:5000/v1/chat/completions"
headers = {"Content-Type": "application/json"}
payload = {
    "model": "Qwen3.6-35B-A3B-uncensored-heretic-Q4_K_S.gguf",
    "messages": [{"role": "user", "content": "hi"}],
    "stream": False
}

try:
    response = requests.post(url, headers=headers, data=json.dumps(payload))
    print(f"Status Code: {response.status_code}")
    print(f"Response: {response.text}")
except Exception as e:
    print(f"Error: {e}")
