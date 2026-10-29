"""OpenCode V2 adapter. All mutating session operations check plugin ownership."""

import base64
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .settings import workspace_path

ORIGIN = "opencode-subagentlink"
REPLY_LIMIT = 4000


class WaitExpired(TimeoutError):
    pass


class Unreachable(RuntimeError):
    """A read could not reach the server; the caller may reconnect."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_id(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(prefix + r"_[A-Za-z0-9_-]+", value):
        raise ValueError(f"Invalid {prefix} ID.")
    return value


def default_title(text):
    line = next((item.strip() for item in text.splitlines() if item.strip()), "task")
    return "Claude: " + (line if len(line) <= 72 else line[:71] + "…")


class Client:
    def __init__(self, url, password):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or not parsed.port
                or parsed.username or parsed.password or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment):
            raise ValueError("The OpenCode server must be a local HTTP endpoint such as http://127.0.0.1:4096.")
        if not isinstance(password, str) or not password:
            raise ValueError("An OpenCode server password is required.")
        self.url = url.rstrip("/")
        self.password = password
        self.deadline = None
        # Never send localhost credentials through an environment-configured proxy.
        self.opener = build_opener(ProxyHandler({}), NoRedirect)

    def api(self, method, path, body=None, timeout=30):
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise WaitExpired()
            timeout = min(timeout, remaining)
        token = base64.b64encode(("opencode:" + self.password).encode()).decode()
        headers = {"Accept": "application/json", "Authorization": "Basic " + token}
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode()
        request = Request(self.url + path, data=payload, headers=headers, method=method.upper())
        try:
            with self.opener.open(request, timeout=timeout) as response:
                if self.deadline is None:
                    raw = response.read()
                else:
                    chunks = []
                    while True:
                        if time.monotonic() >= self.deadline:
                            raise WaitExpired()
                        chunk = response.read1(65536)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                value = json.loads(raw)
        except HTTPError as exc:
            # Error bodies can include provider credentials, prompts, or private paths.
            code = exc.code
            exc.close()
            raise RuntimeError(f"OpenCode HTTP {code} for {method.upper()} {path.split('?')[0]}") from None
        except WaitExpired:
            raise
        except (TimeoutError, URLError, ConnectionError):
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise WaitExpired() from None
            if method.lower() != "get":
                raise RuntimeError("OpenCode request failed; the change may have been accepted. "
                                   "Inspect the session before retrying.") from None
            raise Unreachable(f"OpenCode server at {self.url} could not be reached.") from None
        if not isinstance(value, dict):
            raise RuntimeError("Incompatible OpenCode API response; expected a JSON object.")
        if "error" in value or str(value.get("_tag", "")).endswith("Error"):
            raise RuntimeError("OpenCode returned an API error; inspect the session before retrying.")
        return value

    def data(self, method, path, body=None):
        value = self.api(method, path, body)
        if "data" not in value:
            raise RuntimeError("Incompatible OpenCode API response; missing data field.")
        return value["data"]

    def agents(self, workspace):
        query = urlencode({"location[directory]": workspace_path(workspace)})
        entries = self.data("get", "/api/agent?" + query)
        if not entries:
            time.sleep(1)
            entries = self.data("get", "/api/agent?" + query)
        if not isinstance(entries, list):
            raise RuntimeError("Incompatible OpenCode agent registry.")
        default = None
        if any(not a.get("model") and not a.get("hidden") for a in entries):
            default = self.data("get", "/api/model/default?" + query)
        default_ref = ({k: default[k] for k in ("id", "providerID")} if default else None)
        return [{"id": a["id"], "description": a.get("description"), "mode": a.get("mode"),
                 "model": a.get("model") or default_ref,
                 "model_source": "agent" if a.get("model") else (
                     "workspace_default" if default_ref else "unconfigured")}
                for a in entries if not a.get("hidden")]

    def session(self, sid):
        validate_id(sid, "ses")
        info = self.data("get", f"/api/session/{sid}")
        if not info or info.get("metadata", {}).get("bridge") != ORIGIN:
            raise ValueError("Session was not created by this plugin.")
        return info

    def spawn(self, workspace, text, agent, title=None):
        workspace = workspace_path(workspace)
        if not text.strip():
            raise ValueError("Supply a nonempty task.")
        if not agent:
            raise ValueError("Choose an agent explicitly or save a default during setup.")
        chosen = next((a for a in self.agents(workspace) if a["id"] == agent), None)
        if not chosen or not chosen.get("model"):
            raise ValueError("Agent is unavailable or has no configured/default model. Use agents to inspect.")
        body = {"title": title or default_title(text), "agent": agent, "location": {"directory": workspace},
                "metadata": {"bridge": ORIGIN}}
        if chosen["model_source"] == "agent":
            body["model"] = chosen["model"]
        # Let OpenCode apply the full inherited selection (including options
        # not exposed by /api/model/default), rather than inventing a model ref.
        info = self.data("post", "/api/session", body)
        sid = info["id"]
        try:
            admitted = self.send(sid, text)
        except Exception as exc:
            raise RuntimeError(f"Session created: {sid}; prompt admission uncertain. "
                               "Attach and inspect before retrying.") from exc
        return {**admitted, "agent": agent, "model": info.get("model") or chosen["model"],
                "model_source": chosen["model_source"]}

    def send(self, sid, text, delivery="steer"):
        self.session(sid)
        if delivery not in ("steer", "queue") or not text.strip():
            raise ValueError("Supply nonempty text and delivery steer or queue.")
        admitted = self.data("post", f"/api/session/{sid}/prompt", {"text": text, "delivery": delivery})
        return {"session_id": sid, "message_id": admitted["id"], "delivery": admitted["delivery"]}

    def messages(self, sid, after=None):
        if after:
            validate_id(after, "msg")
        query = {"order": "desc", "limit": "100"}
        found, cursors = [], set()
        while True:
            page = self.api("get", f"/api/session/{sid}/message?" + urlencode(query))
            for message in page["data"]:
                if message["id"] == after:
                    return list(reversed(found))
                found.append(message)
            cursor = page.get("cursor", {}).get("next")
            if not cursor:
                if after:
                    if any(item["id"] == after for item in self.data("get", f"/api/session/{sid}/inbox")):
                        return []
                    raise ValueError("Message cursor was not found in this session.")
                return list(reversed(found))
            if cursor in cursors:
                raise RuntimeError("OpenCode returned a repeated pagination cursor.")
            cursors.add(cursor)
            query = {"cursor": cursor, "limit": "100"}

    def state(self, sid):
        info = self.session(sid)
        base = f"/api/session/{sid}"
        forms = self.data("get", base + "/form")
        permissions = self.data("get", base + "/permission")
        inbox = self.data("get", base + "/inbox")
        active = self.data("get", "/api/session/active")
        if forms or permissions:
            status = "needs_input"
        elif sid in active:
            status = "running"
        elif inbox:
            status = "pending"
        else:
            status = info.get("outcome", "idle")
        return {"session_id": sid, "status": status, "agent": info.get("agent"),
                "model": info.get("model"), "cost": info.get("cost"),
                "forms": forms, "permissions": permissions, "inbox": inbox}

    def busy(self, sids):
        """True while any of these sessions runs, has queued input, or waits for an answer."""
        active = self.data("get", "/api/session/active")
        for sid in sids:
            if sid in active:
                return True
            base = f"/api/session/{sid}"
            if any(self.data("get", base + part) for part in ("/inbox", "/form", "/permission")):
                return True
        return False

    def report(self, sid, after=None):
        status = self.state(sid)
        replies = []
        for item in self.messages(sid, after):
            if item["type"] == "assistant":
                if "completed" not in item["time"]:
                    break
                text = "\n".join(p["text"] for p in item.get("content", []) if p["type"] == "text")
                if text or item.get("error"):
                    replies.append({"id": item["id"], "text": text, "error": item.get("error")})
            if item["type"] == "idle":
                status["idle_id"] = item["id"]
                if item.get("error"):
                    status["error"] = item["error"]
            status["cursor"] = item["id"]
        status.setdefault("cursor", after)
        status["messages"] = replies
        return status

    def wait(self, sid, after, timeout=240, interval=3):
        """Poll until a final state or pending input. timeout=None waits without a limit."""
        validate_id(after, "msg")
        if (timeout is not None and timeout <= 0) or interval < 1:
            raise ValueError("Timeout must be positive and interval at least one second.")
        deadline = None if timeout is None else time.monotonic() + timeout
        previous_idle = None
        previous_deadline, self.deadline = self.deadline, deadline
        result = {"session_id": sid, "status": "unknown", "messages": []}
        try:
            while True:
                result = self.report(sid, after)
                if result["status"] == "needs_input":
                    return result
                if result["status"] not in ("running", "pending"):
                    idle = result.get("idle_id")
                    if idle and idle == previous_idle:
                        return result
                    previous_idle = idle
                else:
                    previous_idle = None
                if deadline is None:
                    time.sleep(interval)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {**result, "wait_timeout": True}
                time.sleep(min(interval, remaining))
        except WaitExpired:
            return {**result, "wait_timeout": True}
        finally:
            self.deadline = previous_deadline

    def interrupt(self, sid, resume=False):
        self.session(sid)
        return self.api("post", f"/api/session/{sid}/interrupt?resume={str(resume).lower()}")

    def reply(self, sid, form_id, answer):
        self.session(sid)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", form_id):
            raise ValueError("Invalid form ID.")
        if not isinstance(answer, dict):
            raise ValueError("Answers must be an object keyed by field IDs.")
        if not any(f["id"] == form_id for f in self.data("get", f"/api/session/{sid}/form")):
            raise ValueError("Form is not pending in this session.")
        return self.api("post", f"/api/session/{sid}/form/{form_id}/reply", {"answer": answer})

    def permission(self, sid, request_id, decision):
        self.session(sid)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", request_id) or decision not in ("once", "reject"):
            raise ValueError("Invalid permission ID or decision.")
        if not any(p["id"] == request_id for p in self.data("get", f"/api/session/{sid}/permission")):
            raise ValueError("Permission is not pending in this session.")
        return self.api("post", f"/api/session/{sid}/permission/{request_id}/reply", {"decision": decision})


def brief(report, limit=REPLY_LIMIT):
    """Outcome for Claude's context: status, pending input, and only the final reply."""
    out = {k: report[k] for k in ("session_id", "status", "error", "wait_timeout", "forms", "permissions")
           if report.get(k) not in (None, [], {})}
    replies = [] if report.get("wait_timeout") else report.get("messages", [])
    if replies:
        last = replies[-1]
        final = {"id": last["id"], "chars": len(last["text"]), "text": last["text"][:limit]}
        if len(last["text"]) > limit:
            final["truncated"] = True
        if last.get("error"):
            final["error"] = last["error"]
        out["final_reply"] = final
        if len(replies) > 1:
            out["earlier_replies"] = len(replies) - 1
    return out
