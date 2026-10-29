from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shlex
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

import opencode_subagentlink
from opencode_subagentlink import settings
from opencode_subagentlink.api import Client, WaitExpired, brief, default_title
from opencode_subagentlink.mcp import TOOLS, Bridge, dispatch, setup_status, validate
from opencode_subagentlink.runtime import PrivateServer, connect_private, run_path

ROOT = Path(__file__).resolve().parents[1]


class Isolated(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.environment = patch.dict(os.environ, {settings.HOME_ENV: str(self.root / "plugin-data"),
                                                   "FAKE_OPENCODE_STATE": str(self.root / "fake-state")})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for key in settings.PASSWORD_KEYS:
            os.environ.pop(key, None)

    def fake_binary(self):
        binary = self.root / "opencode"
        body = Path(__file__).with_name("fake_opencode.py").read_text(encoding="utf-8")
        binary.write_text("#!" + sys.executable + "\n" + body, encoding="utf-8")
        binary.chmod(0o700)
        return str(binary)

    def secret_file(self, text, mode=0o600):
        path = self.root / "server.env"
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return str(path)

    def chdir(self, path):
        previous = os.getcwd()
        os.chdir(path)
        self.addCleanup(os.chdir, previous)


class SettingsTests(Isolated):
    @unittest.skipIf(os.name == "nt", "POSIX fake executable; native Windows needs acceptance testing")
    def test_checklist_asks_consent_then_agent_and_starts_nothing(self):
        binary = self.fake_binary()
        settings.configure(opencode=binary)
        status = setup_status()
        self.assertFalse(status["ready"])
        self.assertEqual([m["key"] for m in status["missing"]], ["consent", "default_agent"])
        self.assertEqual(status["detected"]["opencode_version"], "2.2.1")
        self.assertEqual(status["detected"]["service"], "stopped")
        self.assertIn("background service", status["missing"][0]["ask"])
        self.assertFalse((self.root / "fake-state/service.json").exists())
        settings.configure(consent=True, default_agent="worker")
        self.assertTrue(setup_status()["ready"])

    def test_missing_opencode_is_asked_first(self):
        with patch.object(settings, "find_opencode", side_effect=ValueError("not found")):
            status = setup_status()
        self.assertEqual(status["missing"][0]["key"], "opencode")
        self.assertIn("https://opencode.ai", status["missing"][0]["ask"])

    @unittest.skipIf(os.name == "nt", "POSIX fake executable; native Windows needs acceptance testing")
    def test_private_mode_consent_says_workers_stop_with_claude(self):
        settings.configure(opencode=self.fake_binary(), server="private")
        self.assertIn("interrupts running workers", setup_status()["missing"][0]["ask"])

    def test_consent_can_only_be_granted_and_types_are_checked(self):
        with self.assertRaises(ValueError):
            settings.configure(consent=False)
        settings.private_json(settings.config_path(), {"schema_version": 1, "consent": "yes"})
        with self.assertRaises(ValueError):
            settings.require_setup()

    def test_server_setting_accepts_only_known_modes_and_local_urls(self):
        for value in ("service", "private", "http://127.0.0.1:4096", "http://localhost:4096/"):
            settings.validate_server(value)
        for value in ("https://127.0.0.1:4096", "http://example.com:4096", "http://127.0.0.1:4096/x", "other"):
            with self.assertRaises(ValueError):
                settings.validate_server(value)

    def test_password_file_formats_are_read_but_never_stored(self):
        for text, expected in (("OPENCODE_SERVER_PASSWORD=abc\n", "abc"),
                               ("# comment\nexport OPENCODE_PASSWORD='x y'\n", "x y"),
                               ("bare-secret\n", "bare-secret")):
            self.assertEqual(settings.read_password(self.secret_file(text)), expected)
        with self.assertRaises(ValueError):
            settings.read_password(self.secret_file("OTHER=1\n"))
        settings.configure(password_file=self.secret_file("OPENCODE_PASSWORD=hidden-value\n"))
        self.assertNotIn("hidden-value", settings.config_path().read_text())

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_password_file_must_be_private(self):
        with self.assertRaises(ValueError):
            settings.read_password(self.secret_file("OPENCODE_PASSWORD=x\n", mode=0o644))

    def test_workspace_must_be_absolute_directory(self):
        with self.assertRaises(ValueError):
            settings.workspace_path("relative")
        with self.assertRaises(ValueError):
            settings.workspace_path(__file__)
        self.assertEqual(settings.workspace_path(str(self.root)), str(self.root))

    def test_runtime_id_cannot_escape_state_directory(self):
        with self.assertRaises(ValueError):
            run_path("../config")


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.client = Client("http://127.0.0.1:32123", "test-secret")

    def test_rejects_remote_endpoint_and_missing_password(self):
        for url in ("http://example.com:1234", "http://127.0.0.1:1234/other", "http://user@127.0.0.1:1234"):
            with self.assertRaises(ValueError):
                Client(url, "secret")
        with self.assertRaises(ValueError):
            Client("http://127.0.0.1:1234", "")
        Client("http://localhost:1234", "secret")

    def test_session_ownership_and_path_validation(self):
        with patch.object(self.client, "data", return_value={"metadata": {"bridge": "other"}}) as data:
            with self.assertRaises(ValueError):
                self.client.session("ses_valid")
            data.assert_called_once()
        with patch.object(self.client, "data") as data:
            with self.assertRaises(ValueError):
                self.client.session("ses_../other")
            data.assert_not_called()

    def test_wait_ignores_progress_and_retains_original_cursor(self):
        progress = {"status": "running", "messages": [{"text": "progress"}]}
        done = {"status": "succeeded", "idle_id": "msg_done", "messages": [{"text": "final"}]}
        with patch.object(self.client, "report", side_effect=[progress, done, done]) as report, patch("time.sleep"):
            self.assertEqual(self.client.wait("ses_test", "msg_start"), done)
            self.assertEqual(report.call_count, 3)
            self.assertTrue(all(c.args == ("ses_test", "msg_start") for c in report.call_args_list))

    def test_wait_without_timeout_runs_until_final_state(self):
        done = {"status": "succeeded", "idle_id": "msg_done", "messages": []}
        running = {"status": "running", "messages": []}
        with patch.object(self.client, "report", side_effect=[running] * 50 + [done, done]), patch("time.sleep"):
            self.assertEqual(self.client.wait("ses_test", "msg_start", timeout=None), done)

    def test_wait_requires_post_cursor_idle_and_suppresses_partial_timeout_output(self):
        stale = {"session_id": "ses_test", "status": "succeeded", "messages": [{"id": "msg_a", "text": "partial"}]}
        with patch.object(self.client, "report", return_value=stale), patch("time.monotonic", side_effect=[0, 2]):
            result = self.client.wait("ses_test", "msg_start", timeout=1)
        self.assertTrue(result["wait_timeout"])
        self.assertNotIn("final_reply", brief(result))

    def test_wait_returns_input_and_resets_stability_on_activity(self):
        request = {"status": "needs_input", "permissions": [{"id": "perm_test"}]}
        with patch.object(self.client, "report", return_value=request):
            self.assertEqual(self.client.wait("ses_test", "msg_start"), request)
        done = {"status": "failed", "idle_id": "msg_done"}
        with patch.object(self.client, "report", side_effect=[done, {"status": "running"}, done, done]) as report, patch("time.sleep"):
            self.assertEqual(self.client.wait("ses_test", "msg_start"), done)
            self.assertEqual(report.call_count, 4)

    def test_brief_keeps_only_the_final_reply_and_cuts_long_text(self):
        report = {"session_id": "ses_test", "status": "succeeded", "cursor": "msg_2", "inbox": [],
                  "messages": [{"id": "msg_1", "text": "narration", "error": None},
                               {"id": "msg_2", "text": "x" * 5000, "error": None}]}
        out = brief(report, limit=4000)
        self.assertEqual(out["final_reply"]["id"], "msg_2")
        self.assertEqual(len(out["final_reply"]["text"]), 4000)
        self.assertEqual(out["final_reply"]["chars"], 5000)
        self.assertTrue(out["final_reply"]["truncated"])
        self.assertEqual(out["earlier_replies"], 1)
        self.assertNotIn("narration", json.dumps(out))
        self.assertNotIn("inbox", out)

    def test_default_title_uses_first_line_of_task(self):
        self.assertEqual(default_title("\n  Fix the parser\nDetails"), "Claude: Fix the parser")
        self.assertEqual(len(default_title("y" * 200)), len("Claude: ") + 72)

    def test_partial_reply_does_not_advance_cursor(self):
        messages = [{"id": "msg_user", "type": "user"},
                    {"id": "msg_partial", "type": "assistant", "time": {}, "content": []}]
        with patch.object(self.client, "state", return_value={"status": "running"}), patch.object(self.client, "messages", return_value=messages):
            result = self.client.report("ses_test")
        self.assertEqual(result["cursor"], "msg_user")
        self.assertEqual(result["messages"], [])

    def test_pages_to_cursor_and_rejects_unknown_or_repeated_cursor(self):
        pages = [{"data": [{"id": "msg_3"}], "cursor": {"next": "page2"}},
                 {"data": [{"id": "msg_2"}, {"id": "msg_1"}], "cursor": {"next": None}}]
        with patch.object(self.client, "api", side_effect=pages):
            self.assertEqual(self.client.messages("ses_test", "msg_1"), [{"id": "msg_2"}, {"id": "msg_3"}])
        with patch.object(self.client, "api", return_value={"data": []}), patch.object(self.client, "data", return_value=[]):
            with self.assertRaises(ValueError):
                self.client.messages("ses_test", "msg_missing")
        with patch.object(self.client, "api", return_value={"data": [], "cursor": {"next": "same"}}):
            with self.assertRaises(RuntimeError):
                self.client.messages("ses_test")

    def test_admitted_prompt_can_precede_projected_message(self):
        with patch.object(self.client, "api", return_value={"data": []}), patch.object(self.client, "data", return_value=[{"id": "msg_pending"}]):
            self.assertEqual(self.client.messages("ses_test", "msg_pending"), [])

    def test_state_reads_session_once_and_pending_input_overrides_outcome(self):
        with patch.object(self.client, "session", return_value={"outcome": "succeeded"}) as session, patch.object(self.client, "data", side_effect=[[], [], [{"id": "msg_pending"}], {}]):
            self.assertEqual(self.client.state("ses_test")["status"], "pending")
            session.assert_called_once()
        with patch.object(self.client, "session", return_value={}), patch.object(self.client, "data", side_effect=[[{"id": "form_test"}], [], [], {"ses_test": {}}]):
            self.assertEqual(self.client.state("ses_test")["status"], "needs_input")

    def test_busy_while_running_or_waiting_for_an_answer(self):
        with patch.object(self.client, "data", side_effect=[{"ses_a": {}}]):
            self.assertTrue(self.client.busy(["ses_a"]))
        with patch.object(self.client, "data", side_effect=[{}, [], [{"id": "perm_x"}]]):
            self.assertTrue(self.client.busy(["ses_a"]))
        with patch.object(self.client, "data", side_effect=[{}, [], [], []]):
            self.assertFalse(self.client.busy(["ses_a"]))

    def test_permission_is_validated_without_automatic_approval(self):
        with patch.object(self.client, "session"), patch.object(self.client, "data", return_value=[]), patch.object(self.client, "api") as api:
            with self.assertRaises(ValueError):
                self.client.permission("ses_test", "perm_test", "once")
            api.assert_not_called()
        with patch.object(self.client, "session"), patch.object(self.client, "data", return_value=[{"id": "perm_test"}]), patch.object(self.client, "api") as api:
            self.client.permission("ses_test", "perm_test", "reject")
            self.assertEqual(api.call_args.args[2], {"decision": "reject"})

    def test_idle_error_is_preserved(self):
        with patch.object(self.client, "state", return_value={"status": "failed"}), patch.object(self.client, "messages", return_value=[{"type": "idle", "id": "msg_idle", "error": "provider failed"}]):
            self.assertEqual(self.client.report("ses_test")["error"], "provider failed")


class MCPTests(Isolated):
    def test_initialize_and_tool_listing_have_no_side_effects(self):
        bridge = Bridge()
        with patch("opencode_subagentlink.mcp.Link.get") as get:
            response = dispatch(bridge, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
            self.assertEqual(response["result"]["protocolVersion"], "2025-11-25")
            self.assertEqual(response["result"]["serverInfo"]["name"], settings.NAME)
            listed = dispatch(bridge, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            names = {x["name"] for x in listed["result"]["tools"]}
            self.assertEqual(names, {"setup_status", "configure", "agents", "spawn", "attach", "send", "status",
                                     "result", "wait", "interrupt", "reply", "permission"})
            get.assert_not_called()
        self.assertFalse(settings.home().exists())

    def test_strict_validation(self):
        for name, args in (("configure", {"consent": False}),
                           ("configure", {"idle_minutes": "10"}),
                           ("configure", {"idle_minutes": 0.5}),
                           ("wait", {"session_id": "ses_test", "after": "msg_test", "timeout": True}),
                           ("wait", {"session_id": "ses_test", "after": "msg_test", "timeout": 21}),
                           ("agents", {"workspace": str(self.root), "extra": 1})):
            with self.assertRaises(ValueError):
                validate(name, args)
        validate("configure", {"idle_minutes": 2.5})

    def test_worker_tools_require_consent(self):
        with self.assertRaisesRegex(ValueError, "setup_status"):
            Bridge().invoke("agents", {})

    def test_invalid_protocol_requests_and_tool_errors(self):
        bridge = Bridge()
        self.assertEqual(dispatch(bridge, [])["error"]["code"], -32600)
        self.assertIsNone(dispatch(bridge, {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        value = dispatch(bridge, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "unknown"}})
        self.assertTrue(value["result"]["isError"])

    def test_session_requires_attachment_in_this_conversation(self):
        bridge = Bridge()
        with patch.object(bridge.link, "get") as get:
            with self.assertRaises(ValueError):
                bridge.invoke("status", {"session_id": "ses_test"})
            get.return_value.state.return_value = {"status": "idle"}
            bridge.invoke("attach", {"session_id": "ses_test"})
            bridge.invoke("status", {"session_id": "ses_test"})
            self.assertEqual(get.return_value.state.call_count, 2)

    def test_spawn_defaults_to_current_project_and_returns_background_command(self):
        settings.configure(consent=True, default_agent="worker")
        self.chdir(self.root)
        bridge = Bridge()
        with patch.object(bridge.link, "get") as get:
            get.return_value.spawn.return_value = {"session_id": "ses_x", "message_id": "msg_x", "delivery": "steer"}
            result = bridge.invoke("spawn", {"text": "Do the thing"})
        self.assertEqual(get.return_value.spawn.call_args.args[:3], (str(self.root), "Do the thing", "worker"))
        argv = shlex.split(result["completion"]["background_command"])
        self.assertIn("wait", argv[:4])  # launcher, python + launcher, or python -m package
        for flag, value in (("--home", str(settings.home())), ("--session-id", "ses_x"), ("--after", "msg_x")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("ses_x", bridge.attached)

    def test_changing_server_resets_the_connection(self):
        settings.configure(opencode=self.fake_binary())
        bridge = Bridge()
        with patch.object(bridge.link, "close") as close:
            bridge.invoke("configure", {"default_agent": "worker"})
            close.assert_not_called()
            bridge.invoke("configure", {"server": "private"})
            close.assert_called_once()


class LocalHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind does a reverse DNS lookup, which takes ~30 s on macOS hosts without an FQDN.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.target_calls = []
        calls = self.target_calls

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                calls.append(self.path)
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/target")
                    self.end_headers()
                elif self.path == "/slow":
                    time.sleep(2)
                    self.send_response(200)
                    self.end_headers()
                else:
                    data = json.dumps({"data": "x" * 1_000_000}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

        self.server = LocalHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = Client(f"http://127.0.0.1:{self.server.server_port}", "secret")

    def test_large_response_is_not_truncated(self):
        self.assertEqual(len(self.client.data("get", "/large")), 1_000_000)

    def test_redirect_does_not_forward_credentials(self):
        with self.assertRaises(RuntimeError):
            self.client.api("get", "/redirect")
        self.assertEqual(self.target_calls, ["/redirect"])

    def test_wait_budget_bounds_an_unresponsive_http_request(self):
        def blocked_report(sid, after):
            return self.client.api("get", "/slow")
        start = time.monotonic()
        with patch.object(self.client, "report", side_effect=blocked_report):
            result = self.client.wait("ses_test", "msg_start", timeout=0.15)
        self.assertTrue(result["wait_timeout"])
        self.assertLess(time.monotonic() - start, 0.8)
        self.assertIsNone(self.client.deadline)

    def test_timeout_of_a_budget_bounded_request_is_budget_expiry(self):
        # Socket timers and time.monotonic can disagree by a clock tick (Windows), so the timeout may
        # fire just before the deadline; it must still read as budget expiry, not an unreachable server.
        self.client.deadline = time.monotonic() + 5
        try:
            with patch.object(self.client.opener, "open", side_effect=URLError(TimeoutError("timed out"))):
                with self.assertRaises(WaitExpired):
                    self.client.api("get", "/large")
            with patch.object(self.client.opener, "open", side_effect=URLError(ConnectionRefusedError())):
                with self.assertRaisesRegex(RuntimeError, "could not be reached"):
                    self.client.api("get", "/large")
        finally:
            self.client.deadline = None


@unittest.skipIf(os.name == "nt", "POSIX fake executable; native Windows needs acceptance testing")
class RuntimeTests(Isolated):
    def setUp(self):
        super().setUp()
        self.binary = self.fake_binary()

    def port_closed(self, url):
        with socket.socket() as sock:
            return sock.connect_ex(("127.0.0.1", urlsplit(url).port)) != 0

    def mcp(self, requests):
        data = "".join(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": item}) + "\n"
                       for i, item in enumerate(requests))
        process = subprocess.run([str(ROOT / "scripts" / settings.NAME), "mcp"], input=data, text=True,
                                 encoding="utf-8", capture_output=True, timeout=30, cwd=self.root)
        self.assertEqual(process.returncode, 0, process.stderr)
        responses = [json.loads(line)["result"] for line in process.stdout.splitlines()]
        self.assertTrue(all(not r.get("isError") for r in responses), responses)
        return [json.loads(r["content"][0]["text"]) for r in responses]

    def run_command(self, command):
        return subprocess.run(shlex.split(command), capture_output=True, text=True, encoding="utf-8", timeout=20)

    def test_private_server_is_authenticated_discovers_agents_and_cleans_up(self):
        server = PrivateServer(self.binary, startup_timeout=5)
        self.addCleanup(server.close)
        summary = server.start(str(self.root))
        self.assertNotIn("password", json.dumps(summary))
        with self.assertRaises(HTTPError) as error:
            urlopen(server.client.url + "/api/info", timeout=2)
        self.assertEqual(error.exception.code, 401)
        error.exception.close()
        self.assertIsNotNone(connect_private(server.run_id))
        agents = server.client.agents(str(self.root))
        self.assertEqual([a["id"] for a in agents], ["worker", "inherited"])
        self.assertEqual(agents[1]["model"], {"id": "default-fixture", "providerID": "test"})
        result = server.client.spawn(str(self.root), "你好", "inherited")
        self.assertEqual(result["model"], agents[1]["model"])
        self.assertNotIn("model", server.client.session(result["session_id"]))
        self.assertEqual(server.client.session(result["session_id"])["title"], "Claude: 你好")
        self.assertEqual(stat.S_IMODE(run_path(server.run_id).stat().st_mode), 0o600)
        os.chmod(run_path(server.run_id), 0o644)
        with self.assertRaises(ValueError):
            connect_private(server.run_id)
        os.chmod(run_path(server.run_id), 0o600)
        url = server.client.url
        server.close()
        self.assertFalse(run_path(server.run_id).exists())
        self.assertTrue(self.port_closed(url))

    def test_failed_start_and_timeout_clean_up(self):
        for mode in ("exit", "hang"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"FAKE_OPENCODE_MODE": mode}):
                server = PrivateServer(self.binary, startup_timeout=0.4)
                with self.assertRaises(RuntimeError):
                    server.start(str(self.root))
                self.assertIsNone(server.process)
                self.assertFalse(run_path(server.run_id).exists())
                self.assertTrue(server.log_path.exists())

    def test_service_mode_spawn_and_background_wait_through_the_launcher(self):
        self.addCleanup(subprocess.run, [self.binary, "service", "stop"], capture_output=True)
        setup, spawned = self.mcp([
            {"name": "configure", "arguments": {"opencode": self.binary, "consent": True, "default_agent": "worker"}},
            {"name": "spawn", "arguments": {"text": "hello"}}])
        self.assertTrue(setup["ready"])
        self.assertEqual(spawned["server"]["server"], "service")
        self.assertNotIn("--run-id", spawned["completion"]["background_command"])
        # The service outlives the MCP process; a later Bash command finds it from settings.
        waited = self.run_command(spawned["completion"]["background_command"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        outcome = json.loads(waited.stdout)
        self.assertEqual(outcome["status"], "succeeded")
        self.assertEqual(outcome["final_reply"]["text"], "Fixture reply: hello")
        self.assertEqual(outcome["earlier_replies"], 1)
        full = json.loads(self.run_command(spawned["completion"]["result_command"]).stdout)
        self.assertEqual([m["text"] for m in full["messages"]], ["Reading files first.", "Fixture reply: hello"])

    def test_url_mode_uses_password_file_and_rejects_a_wrong_one(self):
        port = socket.socket()
        port.bind(("127.0.0.1", 0))
        number = port.getsockname()[1]
        port.close()
        server = subprocess.Popen([self.binary, "serve", "--port", str(number)],
                                  env={**os.environ, "OPENCODE_PASSWORD": "pw-123"},
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(server.wait, 5)
        self.addCleanup(server.terminate)
        for _ in range(100):
            if not self.port_closed(f"http://127.0.0.1:{number}"):
                break
            time.sleep(0.05)
        url = f"http://127.0.0.1:{number}"
        bridge = Bridge()
        self.addCleanup(bridge.close)
        bridge.invoke("configure", {"server": url, "password_file": self.secret_file("OPENCODE_SERVER_PASSWORD=pw-123\n"),
                                    "consent": True, "default_agent": "worker"})
        self.assertEqual(bridge.invoke("agents", {})["server"]["url"], url)
        bridge.invoke("configure", {"password_file": self.secret_file("OPENCODE_PASSWORD=wrong\n")})
        with self.assertRaisesRegex(RuntimeError, "401"):
            bridge.invoke("agents", {})

    def test_private_mode_mcp_eof_stops_server_after_worker_result(self):
        _, _, spawned, waited = self.mcp([
            {"name": "configure", "arguments": {"opencode": self.binary, "server": "private", "consent": True,
                                                "default_agent": "worker"}},
            {"name": "agents", "arguments": {"workspace": str(self.root)}},
            {"name": "spawn", "arguments": {"text": "hello"}},
            {"name": "wait", "arguments": {"session_id": "ses_fixture", "after": "msg_user", "timeout": 4}}])
        self.assertIn("--run-id", spawned["completion"]["background_command"])
        self.assertEqual(waited["final_reply"]["text"], "Fixture reply: hello")
        self.assertFalse(list((settings.home() / "runs").glob("*.json")))
        self.assertTrue(self.port_closed(spawned["server"]["url"]))

    def test_idle_private_server_stops_and_restarts_on_next_call(self):
        settings.configure(opencode=self.binary, server="private", consent=True, default_agent="worker")
        bridge = Bridge()
        self.addCleanup(bridge.close)
        first = bridge.invoke("agents", {"workspace": str(self.root)})["server"]
        self.assertFalse(bridge.idle_check())
        with patch.object(bridge.link.client, "busy", return_value=True):
            self.assertFalse(bridge.idle_check(now=time.monotonic() + 3600))
        self.assertTrue(bridge.idle_check(now=time.monotonic() + 3600))
        self.assertTrue(self.port_closed(first["url"]))
        second = bridge.invoke("agents", {"workspace": str(self.root)})["server"]
        self.assertNotEqual(first["run_id"], second["run_id"])

    def test_abrupt_mcp_death_closes_pipe_and_stops_server(self):
        code = """import sys,time
from opencode_subagentlink.runtime import PrivateServer
r=PrivateServer(sys.argv[1], startup_timeout=5)
r.start(sys.argv[2])
print(r.run_id, flush=True)
time.sleep(60)
"""
        process = subprocess.Popen([sys.executable, "-c", code, self.binary, str(self.root)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.stderr.close)
        # A bounded read via select avoids hanging the test if startup regresses.
        import select
        self.assertTrue(select.select([process.stdout], [], [], 8)[0])
        run_id = process.stdout.readline().strip()
        record = json.loads(run_path(run_id).read_text())
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + 8
        while run_path(run_id).exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(run_path(run_id).exists())
        self.assertTrue(self.port_closed(record["url"]))

    @unittest.skipUnless(sys.platform == "linux", "Checks Linux process state for group cleanup")
    def test_cleanup_kills_tool_that_ignores_termination(self):
        child_file = self.root / "child.pid"
        with patch.dict(os.environ, {"FAKE_OPENCODE_MODE": "tool", "FAKE_CHILD_PID_FILE": str(child_file)}):
            with PrivateServer(self.binary, startup_timeout=5) as server:
                server.start(str(self.root))
                child_pid = int(child_file.read_text())
            path = Path(f"/proc/{child_pid}/stat")
            deadline = time.monotonic() + 3
            while path.exists() and path.read_text().split()[2] != "Z" and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertTrue(not path.exists() or path.read_text().split()[2] == "Z")


class PluginFilesTests(unittest.TestCase):
    def load(self, relative):
        return json.loads((ROOT / relative).read_text(encoding="utf-8"))

    def test_names_versions_and_paths_agree(self):
        plugin = self.load(".claude-plugin/plugin.json")
        market = self.load(".claude-plugin/marketplace.json")
        servers = plugin["mcpServers"]
        # A root .mcp.json would also load as a project server in a checkout, where the plugin variables are unset.
        self.assertFalse((ROOT / ".mcp.json").exists())
        self.assertEqual(plugin["name"], settings.NAME)
        self.assertEqual(plugin["version"], opencode_subagentlink.__version__)
        self.assertEqual(plugin["license"], "Apache-2.0")
        self.assertEqual([(p["name"], p["source"]) for p in market["plugins"]], [(settings.NAME, "./")])
        self.assertEqual(list(servers), [settings.NAME])
        launcher = "${CLAUDE_PLUGIN_ROOT}/scripts/" + settings.NAME
        self.assertEqual(servers[settings.NAME]["args"], [launcher, "mcp"])
        self.assertEqual(servers[settings.NAME]["env"], {settings.HOME_ENV: "${CLAUDE_PLUGIN_DATA}"})
        self.assertTrue((ROOT / "scripts" / settings.NAME).is_file())
        if os.name != "nt":
            self.assertTrue(os.access(ROOT / "scripts" / settings.NAME, os.X_OK))
        self.assertIn('version = {attr = "opencode_subagentlink.__version__"}',
                      (ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    def test_skill_frontmatter_names_the_skill_and_preapproves_only_read_commands(self):
        text = (ROOT / "skills/delegate/SKILL.md").read_text(encoding="utf-8")
        front = text.split("---")[1]
        self.assertRegex(front, r"(?m)^name: delegate$")
        approved = re.findall(r"opencode-subagentlink (\w+) \*\)", front)
        self.assertEqual(sorted(approved), ["doctor", "result", "status", "wait"])

    def test_tool_descriptions_are_present(self):
        self.assertTrue(all(t["description"] for t in TOOLS))


if __name__ == "__main__":
    unittest.main()
