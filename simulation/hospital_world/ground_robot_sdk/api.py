"""Versioned stdlib HTTP API plus a dependency-free WebSocket telemetry feed."""
from __future__ import annotations
import base64, hashlib, json, struct, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from .models import CommandStatus

class LocalAPIServer:
    def __init__(self, runtime, host="127.0.0.1", port=8080, telemetry_hz=5.):
        self.runtime,self.telemetry_hz=runtime,telemetry_hz; self.httpd=ThreadingHTTPServer((host,port),self._handler()); self.httpd.daemon_threads=True; self.thread=threading.Thread(target=self.httpd.serve_forever,daemon=True)
    @property
    def address(self): return self.httpd.server_address
    def start(self): self.thread.start()
    def shutdown(self): self.httpd.shutdown(); self.httpd.server_close(); self.thread.join(3)
    def _handler(self):
        runtime,rate=self.runtime,self.telemetry_hz
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_): pass
            def do_GET(self):
                path=urlparse(self.path).path
                if path=="/api/v1/telemetry": return self._telemetry()
                if path in ("/health","/api/v1/health"): return self._json(200,{"ok":True})
                if path in ("/robot/state","/api/v1/robot/state"): return self._json(200,runtime.state().to_dict())
                state=runtime.state()
                routes={"/robot/pose":state.odometry.pose.to_dict(),"/api/v1/robot/pose":state.odometry.pose.to_dict(),"/robot/odometry":state.odometry.to_dict(),"/api/v1/robot/odometry":state.odometry.to_dict(),"/robot/range":state.range.to_dict(),"/api/v1/robot/range":state.range.to_dict(),"/api/v1/robot/capabilities":runtime.capabilities().to_dict()}
                if path in routes: return self._json(200,routes[path])
                if path in ("/robot/camera/latest","/api/v1/robot/camera/latest"):
                    camera=state.camera
                    if not camera.available or not camera.data: return self._json(404,{"error":"No camera frame received yet"})
                    self.send_response(200); self.send_header("Content-Type","image/jpeg"); self.send_header("Content-Length",str(len(camera.data))); self.end_headers(); self.wfile.write(camera.data); return
                if path.startswith("/api/v1/commands/"):
                    value=runtime.controller.get_command(path.rsplit("/",1)[-1]); return self._json(200,value.to_dict()) if value else self._json(404,{"error":"Unknown command"})
                self._json(404,{"error":"Unknown endpoint"})
            def do_POST(self):
                path=urlparse(self.path).path; body=self._body()
                try:
                    if path in ("/robot/command","/api/v1/control/velocity"): result=runtime.controller.execute(body,"remote") if path=="/robot/command" else runtime.controller.set_velocity(body.get("linear",0),body.get("angular",0),body.get("duration"),"remote")
                    elif path=="/api/v1/control/drive": result=runtime.motion.drive(body["speed"],body["steering_angle"],body.get("duration"),"remote")
                    elif path in ("/robot/stop","/api/v1/control/stop"): result=runtime.motion.stop("remote")
                    elif path=="/api/v1/control/emergency-stop": result=runtime.motion.stop("emergency",emergency=True)
                    else: return self._json(404,{"error":"Unknown endpoint"})
                except (KeyError,TypeError,ValueError) as exc: return self._json(400,{"error":str(exc)})
                code=202 if result.status in (CommandStatus.ACCEPTED,CommandStatus.RUNNING,CommandStatus.COMPLETED) else 409
                self._json(code,result.to_dict())
            def _body(self):
                length=int(self.headers.get("Content-Length","0")); value=json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(value,dict): raise ValueError("JSON body must be an object")
                return value
            def _json(self,code,value):
                data=json.dumps(value,allow_nan=False).encode(); self.send_response(code); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data)
            def _telemetry(self):
                key=self.headers.get("Sec-WebSocket-Key")
                if self.headers.get("Upgrade","").lower()!="websocket" or not key: return self._json(400,{"error":"WebSocket upgrade required"})
                accept=base64.b64encode(hashlib.sha1((key+"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                self.send_response(101,"Switching Protocols"); self.send_header("Upgrade","websocket"); self.send_header("Connection","Upgrade"); self.send_header("Sec-WebSocket-Accept",accept); self.end_headers()
                try:
                    while True:
                        state=runtime.state(); payload={"timestamp":time.time(),"pose":state.odometry.pose.to_dict(),"velocity":state.odometry.velocity.to_dict(),"front_range":state.range.front,"health":state.health.to_dict(),"controller":state.controller}
                        self.connection.sendall(_frame(json.dumps(payload,separators=(",",":"))))
                        time.sleep(1/max(rate,.1))
                except (BrokenPipeError,ConnectionResetError,OSError): pass
        return Handler

def _frame(text: str) -> bytes:
    payload=text.encode(); size=len(payload)
    if size<126: return b"\x81"+bytes([size])+payload
    if size<65536: return b"\x81\x7e"+struct.pack("!H",size)+payload
    return b"\x81\x7f"+struct.pack("!Q",size)+payload
