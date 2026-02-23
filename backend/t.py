import http.client
import json

conn = http.client.HTTPSConnection("api.302.ai")
payload = json.dumps({
   "audio_url": "http://119.45.51.201/api/videos/44767c2a92df21d4c83303fd0a8777c6/audio",
   "language": "en",
   "demucs": True,
   "is_only_demucs": True
})
headers = {
   'Authorization': 'Bearer sk-cPi8HjzJ2A2tqC6yTiqtj367fTYHF0pXF39FCR9MoRfxtkqd',
   'Content-Type': 'application/json'
}
conn.request("POST", "/302/vt/subtitle/extract", payload, headers)
res = conn.getresponse()
data = res.read()
print(data.decode("utf-8"))