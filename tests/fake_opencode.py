"""Subprocess fixture: a tiny authenticated API and background service, with no models or external calls."""

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import socketserver
import subprocess
import sys
import time
from urllib.parse import urlsplit


class LocalHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind does a reverse DNS lookup, which takes ~30 s on macOS hosts without an FQDN.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    return not stat.exists() or stat.read_text().split()[2] != "Z"


def service(args):
    """Mirror `opencode service start|status|get password|stop` from OpenCode 2.2.1."""
    path = Path(os.environ["FAKE_OPENCODE_STATE"]) / "service.json"
    state = json.loads(path.read_text()) if path.exists() else None
    running = bool(state and alive(state["pid"]))
    if args == ["start"]:
        if not running:
            port, password = free_port(), secrets.token_urlsafe(16)
            process = subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve", "--port", str(port)],
                                       env={**os.environ, "OPENCODE_PASSWORD": password}, start_new_session=True,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(100):
                with socket.socket() as sock:
                    if sock.connect_ex(("127.0.0.1", port)) == 0:
                        break
                time.sleep(0.05)
            state = {"url": f"http://127.0.0.1:{port}", "pid": process.pid, "password": password}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state))
        print(state["url"])
    elif args == ["status"]:
        print(state["url"] if running else "stopped")
    elif args == ["get", "password"]:
        print(state["password"] if state else "")
    elif args == ["stop"]:
        if running:
            os.kill(state["pid"], signal.SIGTERM)
        path.unlink(missing_ok=True)
    else:
        raise SystemExit(2)


def main():
    if "--version" in sys.argv:
        print("opencode v2.2.1")
        return
    if sys.argv[1:2] == ["service"]:
        return service(sys.argv[2:])
    mode = os.environ.get("FAKE_OPENCODE_MODE")
    if mode == "exit":
        raise SystemExit(7)
    if mode == "hang":
        time.sleep(60)
        return
    if mode == "tool":
        # Child ignores SIGTERM; cleanup must remove the whole owned group.
        child = subprocess.Popen([sys.executable, "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
        with open(os.environ["FAKE_CHILD_PID_FILE"], "w") as stream:
            stream.write(str(child.pid))
    port = int(sys.argv[sys.argv.index("--port") + 1])
    token = base64.b64encode(("opencode:" + os.environ["OPENCODE_PASSWORD"]).encode()).decode()
    sessions = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def handle_request(self, method):
            if self.headers.get("Authorization") != "Basic " + token:
                self.send_error(401)
                return
            path = urlsplit(self.path).path
            if path == "/api/info":
                self.respond({"version": "2.2.1", "pid": os.getpid()})
            elif path == "/api/agent":
                self.respond({"data": [
                    {"id": "worker", "description": "Fixture worker", "hidden": False,
                     "model": {"id": "fixture", "providerID": "test"}, "mode": "all"},
                    {"id": "inherited", "description": "Inherits default", "hidden": False},
                    {"id": "internal", "hidden": True}]})
            elif path == "/api/model/default":
                self.respond({"data": {"id": "default-fixture", "providerID": "test"}})
            elif path == "/api/session/active":
                self.respond({"data": {}})
            elif path == "/api/session" and method == "post":
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                sessions["ses_fixture"] = {**body, "id": "ses_fixture", "outcome": "succeeded"}
                self.respond({"data": sessions["ses_fixture"]})
            elif path == "/api/session/ses_fixture":
                self.respond({"data": sessions.get("ses_fixture")})
            elif path.endswith("/prompt"):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                sessions["ses_fixture"]["text"] = body["text"]
                self.respond({"data": {"id": "msg_user", "delivery": body["delivery"]}})
            elif path.endswith("/message"):
                self.respond({"data": [
                    {"id": "msg_idle", "type": "idle"},
                    {"id": "msg_reply", "type": "assistant", "time": {"completed": 1},
                     "content": [{"type": "tool", "name": "read"},
                                 {"type": "text", "text": "Fixture reply: " + sessions["ses_fixture"]["text"]}]},
                    {"id": "msg_note", "type": "assistant", "time": {"completed": 1},
                     "content": [{"type": "text", "text": "Reading files first."}]},
                    {"id": "msg_user", "type": "user"}], "cursor": {"next": None}})
            elif path.endswith(("/inbox", "/form", "/permission")):
                self.respond({"data": []})
            elif path.endswith("/interrupt"):
                self.respond({"data": {}})
            else:
                self.send_error(404)

        def do_GET(self):
            self.handle_request("get")

        def do_POST(self):
            self.handle_request("post")

    LocalHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
