import sys
import json
import urllib.request

text = sys.argv[1] if len(sys.argv) > 1 else "Deliver a wheelchair to ward 3, routine"
msg_id = sys.argv[2] if len(sys.argv) > 2 else "req-demo-001"

payload = {
    "message_id": msg_id,
    "text": text
}

req = urllib.request.Request(
    "http://127.0.0.1:8000/requests",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"}
)

try:
    with urllib.request.urlopen(req) as resp:
        print("Response Code:", resp.status)
        body = json.loads(resp.read().decode())
        print("Response Body:\n", json.dumps(body, indent=2))
except Exception as e:
    print("Error sending request:", e)
