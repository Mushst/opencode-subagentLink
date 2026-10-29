"""Stop the owned server when the MCP process exits, including an abrupt crash.

The parent holds a pipe open. EOF is the lifetime signal; no polling by an LLM
or PID-based parent discovery is needed. The supervisor itself is not a service.
"""

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading


def stop_tree(process):
    if os.name == "nt":
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=10, check=False)
            if process.poll() is None:
                process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=5)
    finally:
        if os.name != "nt":
            # A server may exit before a tool that ignores SIGTERM.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def main():
    record, command = Path(sys.argv[1]), sys.argv[2:]
    stopped = threading.Event()

    def stop(signum, frame):
        stopped.set()

    def watch_parent():
        try:
            # The parent never writes data. A closed pipe means it is gone.
            sys.stdin.buffer.read(1)
        finally:
            stopped.set()

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)
    process = None
    try:
        kwargs = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, **kwargs)
        threading.Thread(target=watch_parent, daemon=True).start()
        while process.poll() is None and not stopped.wait(0.2):
            pass
        return process.returncode or 0
    finally:
        if process is not None:
            stop_tree(process)
        record.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
