import subprocess
import time
import urllib.request
import json
import os

print('Starting ollama serve...')
proc = subprocess.Popen(['/usr/local/bin/ollama', 'serve'])
time.sleep(3)
print('Ollama PID:', proc.pid, 'Poll status:', proc.poll())

try:
    with urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=2.0) as resp:
        print('HTTP Status:', resp.status)
        data = json.loads(resp.read().decode())
        print('Models found:', [m.get('name') for m in data.get('models', [])])
except Exception as e:
    print('Failed to query Ollama API:', e)

# Keep running
try:
    proc.wait()
except KeyboardInterrupt:
    proc.terminate()
