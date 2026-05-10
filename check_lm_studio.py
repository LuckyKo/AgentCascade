import requests
import json

api_base = "http://localhost:1234/v1" # Adjust if yours is different
try:
    response = requests.get(f"{api_base}/models")
    if response.status_code == 200:
        print("Successfully connected to LM Studio!")
        data = response.json()
        print(json.dumps(data, indent=2))
    else:
        print(f"Failed to get models. Status code: {response.status_code}")
        print(response.text)
except Exception as e:
    print(f"Error connecting to LM Studio: {e}")
