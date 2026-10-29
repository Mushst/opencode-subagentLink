"""Connections to an OpenCode V2 server.

service  OpenCode's own background service, shared by every OpenCode client of this user.
         `opencode service start` starts it on demand and prints its address.
URL      A server the user runs, authenticated from a password file.
private  A server owned by this process and stopped with it (see supervisor.py).
"""

import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import time
import uuid

from . import settings
from .api import Client
from .settings import home, private_json, workspace_path


def version(executable):
    result = subprocess.run([executable, "--version"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=15)
    match = re.search(r"\b(?:opencode\s+)?v?(\d+)\.(\d+)\.(\d+)", result.stdout)
    if result.returncode or not match or int(match[1]) != 2:
        raise ValueError("OpenCode V2 is required. Verify this executable with --version.")
    return ".".join(match.groups())


def check_v2(client, timeout=30):
    info = client.api("get", "/api/info", timeout=timeout)
    if str(info.get("version", "")).lstrip("v").split(".")[0] != "2":
        raise RuntimeError("The server did not expose an OpenCode V2 API.")
    return info


def opencode(binary, *args):
    result = subprocess.run([binary, *args], capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=90, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise RuntimeError(f"`opencode {' '.join(args)}` failed with exit code {result.returncode}.")
    return result.stdout.strip()


def service_status(binary):
    """The background service's address, or 'stopped'. Starts nothing."""
    lines = opencode(binary, "service", "status").splitlines()
    return lines[0].strip() if lines else "unknown"


def service_client(binary):
    version(binary)
    output = opencode(binary, "service", "start")  # Idempotent; prints the address.
    url = next((line.strip() for line in output.splitlines() if line.strip().startswith("http")), None)
    if not url:
        raise RuntimeError("`opencode service start` did not report a server address.")
    client = Client(url, opencode(binary, "service", "get", "password"))
    check_v2(client)
    return client


def url_client(url, password_file=None):
    if password_file:
        password = settings.read_password(password_file)
    else:
        password = next((os.environ[k] for k in settings.PASSWORD_KEYS if os.environ.get(k)), None)
    if not password:
        raise ValueError(f"Set password_file for the server at {url}, or OPENCODE_PASSWORD in Claude's environment.")
    client = Client(url, password)
    check_v2(client)
    return client


def run_path(run_id):
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("Invalid run ID.")
    return home() / "runs" / (run_id + ".json")


def connect_private(run_id):
    path = run_path(run_id)
    if path.is_symlink():
        raise ValueError("Runtime records must not be symbolic links.")
    try:
        if os.name != "nt":
            stat = path.stat()
            if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
                raise ValueError("Runtime credentials have unsafe ownership or permissions.")
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError("Private server is gone; its Claude session closed or it stopped while idle.") from None
    client = Client(record["url"], record["password"])
    info = client.api("get", "/api/info")
    if info.get("pid") != record["server_pid"]:
        raise RuntimeError("Runtime record does not match the running OpenCode server.")
    return client


class PrivateServer:
    """One foreground `opencode serve` owned by this process."""

    def __init__(self, executable, startup_timeout=60):
        self.executable = executable
        self.startup_timeout = startup_timeout
        self.process = None
        self.client = None
        self.run_id = uuid.uuid4().hex
        self.info = None
        self.log = None
        self.log_path = None
        self.failed = False

    def start(self, workspace):
        workspace = workspace_path(workspace)
        if self.process is not None:
            if self.process.poll() is not None:
                raise RuntimeError("The private OpenCode server exited.")
            return self.summary()
        installed_version = version(self.executable)
        # serve --port 0 is not a documented V2 contract. Reserve an unused port
        # briefly, then authenticate readiness; a bind race is a startup failure.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        password = secrets.token_urlsafe(32)
        environment = dict(os.environ, OPENCODE_PASSWORD=password)
        environment.pop("OPENCODE_SERVER_PASSWORD", None)
        self.log_path = run_path(self.run_id).with_suffix(".log")
        self.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log = open(self.log_path, "xb")
        os.chmod(self.log_path, 0o600)
        try:
            self.process = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("supervisor.py")), str(run_path(self.run_id)),
                 self.executable, "serve", "--hostname", "127.0.0.1", "--port", str(port)],
                cwd=workspace, env=environment, stdin=subprocess.PIPE,
                stdout=self.log, stderr=self.log)
            self.client = Client(f"http://127.0.0.1:{port}", password)
            deadline = time.monotonic() + self.startup_timeout
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(f"Private OpenCode server exited with code {self.process.returncode}.")
                try:
                    info = self.client.api("get", "/api/info", timeout=1)
                except (RuntimeError, ValueError):
                    time.sleep(0.2)
                    continue
                if str(info.get("version", "")).lstrip("v").split(".")[0] != "2":
                    raise RuntimeError("Private server did not expose an OpenCode V2 API.")
                if not isinstance(info.get("pid"), int):
                    raise RuntimeError("OpenCode /api/info did not identify its server process.")
                self.info = info
                private_json(run_path(self.run_id), {"url": self.client.url, "password": password,
                             "server_pid": info["pid"], "owner_pid": os.getpid()})
                return {**self.summary(), "executable_version": installed_version}
            raise RuntimeError("Private OpenCode server did not become ready before the startup timeout.")
        except BaseException as exc:
            self.failed = isinstance(exc, Exception)
            self.close()
            if self.failed:
                raise RuntimeError(f"{exc} Private startup log: {self.log_path}") from exc
            raise

    def summary(self):
        return {"run_id": self.run_id,
                "server_version": self.info.get("version") if self.info else None}

    def close(self):
        process = self.process
        try:
            if process is not None:
                if process.stdin:
                    process.stdin.close()
                try:
                    process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=12)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
        finally:
            if self.log is not None:
                self.log.close()
                self.log = None
            run_path(self.run_id).unlink(missing_ok=True)
            if self.log_path and not self.failed:
                self.log_path.unlink(missing_ok=True)
            self.process = None
            self.client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


LIFETIME = {
    "service": "OpenCode's background service. Workers keep running if this Claude session closes, "
               "but nothing reports their completion then; reattach later with attach.",
    "url": "Your OpenCode server. Workers keep running if this Claude session closes, "
           "but nothing reports their completion then; reattach later with attach.",
    "private": "A private server for this Claude session. Closing the session stops it and interrupts "
               "running workers. It also stops after {idle} idle minutes and restarts on the next call.",
}


class Link:
    """The OpenCode server for one MCP connection, opened on first use."""

    def __init__(self):
        self.client = None
        self.private = None
        self.mode = None
        self.idle_minutes = settings.DEFAULT_IDLE_MINUTES
        self.used = time.monotonic()

    def get(self, workspace, config=None):
        config = settings.require_setup() if config is None else config
        mode = settings.server_mode(config)
        stale = self.private is not None and (self.private.process is None or self.private.process.poll() is not None)
        if self.client is not None and (mode != self.mode or stale):
            self.close()
        if self.client is None:
            if mode == "private":
                server = PrivateServer(settings.find_opencode(config.get("opencode")))
                server.start(workspace)
                self.private, self.client = server, server.client
            elif mode == "service":
                self.client = service_client(settings.find_opencode(config.get("opencode")))
            else:
                self.client = url_client(mode, config.get("password_file"))
            self.mode = mode
        self.idle_minutes = config.get("idle_minutes", settings.DEFAULT_IDLE_MINUTES)
        self.used = time.monotonic()
        return self.client

    def describe(self):
        if self.client is None:
            return None
        kind = self.mode if self.mode in ("service", "private") else "url"
        out = {"server": self.mode, "url": self.client.url,
               "lifetime": LIFETIME[kind].format(idle=self.idle_minutes)}
        if self.private is not None:
            out["run_id"] = self.private.run_id
        return out

    def cli_args(self):
        return ["--run-id", self.private.run_id] if self.private is not None else []

    def idle_check(self, sessions, limit_seconds, now=None):
        """Stop an idle private server. Shared servers are left alone."""
        now = time.monotonic() if now is None else now
        if self.private is None or now - self.used < limit_seconds:
            return False
        if self.client.busy(sessions):
            return False
        self.close()
        return True

    def close(self):
        if self.private is not None:
            self.private.close()
        self.client = self.private = self.mode = None
