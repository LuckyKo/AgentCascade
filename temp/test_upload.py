import requests

url = "http://localhost:5001/api/parse"
files = {'file': open('n:/work/WD/AgentCascade/scratch/dummy.txt', 'rb')}
response = requests.post(url, files=files)

print("Status Code:", response.status_code)
print("Response:", response.text)
