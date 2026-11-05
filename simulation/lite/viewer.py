#!/usr/bin/env python3
"""Gazebo-less live viewer for the kinematic simulator (P1's hospital map).

Robots running in `--sim-mode kinematic` POST their pose here (NIMBUS_SIM_VIEWER_URL);
the page draws P1's hospital topology and moves the six robots live. It also proxies the
cloud's /fleet and /tasks so the page can show leases next to the map.

    python simulation/lite/viewer.py --port 8090 --cloud http://127.0.0.1:8000
"""
import argparse, json, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOPO_DIR = HERE.parent / "hospital_world" / "topology"
POSES, LOCK = {}, threading.Lock()
NOTES = {}        # optional description per closed zone (e.g. 'unidentified object, unclear what it is')
BLOCKED = set()   # zones the operator clicked closed (kinematic robots treat them as obstacles)
CLOUD = "http://127.0.0.1:8000"


def static_map():
    topo = json.loads((TOPO_DIR / "hospital_topology.json").read_text())
    wps = json.loads((TOPO_DIR / "topology_waypoints.json").read_text())["waypoints"]
    return {"nodes": {n: {"xy": wps[n], "wing": d.get("wing"), "kind": d.get("kind")} for n, d in topo["nodes"].items()},
            "edges": [[a, b] for a, b, *_ in topo["edges"]]}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, (HERE / "viewer.html").read_text(), "text/html")
        elif self.path == "/map.json":
            self._send(200, static_map())
        elif self.path == "/background.png":
            self._send(200, (TOPO_DIR / "hospital_topology.png").read_bytes(), "image/png")
        elif self.path == "/obstacles":
            with LOCK:
                self._send(200, {"blocked": sorted(BLOCKED), "notes": dict(NOTES)})
        elif self.path == "/poses":
            with LOCK:
                self._send(200, {"now": time.time(), "robots": POSES})
        elif self.path.startswith("/cloud/"):
            try:
                with urllib.request.urlopen(CLOUD + "/" + self.path[7:], timeout=2) as r:
                    self._send(200, r.read())
            except Exception as e:
                self._send(502, {"error": str(e)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/pose":
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            with LOCK:
                POSES[d["id"]] = d
            self._send(200, {"ok": True})
        elif self.path == "/obstacle":
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            with LOCK:
                (BLOCKED.add if d.get("blocked", True) else BLOCKED.discard)(d["zone"])
                if d.get("note") and d.get("blocked", True):
                    NOTES[d["zone"]] = d["note"]
                else:
                    NOTES.pop(d["zone"], None)
                self._send(200, {"blocked": sorted(BLOCKED), "notes": dict(NOTES)})
        else:
            self._send(404, {"error": "not found"})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--cloud", default=CLOUD)
    a = ap.parse_args()
    CLOUD = a.cloud.rstrip("/")
    print(f"Kinematic sim viewer: http://127.0.0.1:{a.port}")
    ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever()
