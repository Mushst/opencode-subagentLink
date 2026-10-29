"""Opt-in probes of a real OpenCode V2 with isolated user data and no model calls."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

LAUNCHER = Path(__file__).resolve().parents[1] / "scripts" / "opencode-subagentlink"


@unittest.skipUnless(os.environ.get("BRIDGE_TEST_OPENCODE"), "Set BRIDGE_TEST_OPENCODE to opt into a real V2 probe")
class LiveProbe(unittest.TestCase):
    def setUp(self):
        self.binary = str(Path(os.environ["BRIDGE_TEST_OPENCODE"]).expanduser().resolve(strict=True))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "project").mkdir()
        (self.root / "home").mkdir()
        # Do not inherit provider credentials, production configuration, or the user's systemd session.
        self.environment = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ}
        self.environment.update({
            "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"),
            "APPDATA": str(self.root / "appdata"), "LOCALAPPDATA": str(self.root / "local"),
            "XDG_CONFIG_HOME": str(self.root / "config"), "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_STATE_HOME": str(self.root / "state"), "XDG_CACHE_HOME": str(self.root / "cache"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })

    def run_cli(self, *args):
        process = subprocess.run([sys.executable, str(LAUNCHER), *args, "--home", str(self.root / "plugin")],
                                 capture_output=True, text=True, encoding="utf-8", env=self.environment,
                                 cwd=self.root / "project", timeout=120)
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        return json.loads(process.stdout)

    def probe(self, server):
        self.run_cli("setup", "--consent", "--agent", "build", "--server", server, "--opencode", self.binary)
        result = self.run_cli("doctor", "--probe")
        self.assertTrue(result["ok"])
        self.assertEqual(result["probe"]["server"]["server"], server)
        self.assertTrue(result["probe"]["agents"])
        return result

    def test_private_server_startup_discovery_and_shutdown(self):
        self.probe("private")
        runs = self.root / "plugin/runs"
        self.assertFalse(list(runs.glob("*.json")))
        self.assertFalse(list(runs.glob("*.log")))

    def test_background_service_starts_on_demand_and_is_found_again(self):
        stop = [self.binary, "service", "stop"]
        self.addCleanup(subprocess.run, stop, env=self.environment, capture_output=True, timeout=60)
        first = self.probe("service")["probe"]["server"]["url"]
        status = subprocess.run([self.binary, "service", "status"], env=self.environment,
                                capture_output=True, text=True, timeout=60).stdout.strip()
        self.assertEqual(status, first)
        self.assertEqual(self.run_cli("doctor", "--probe")["probe"]["server"]["url"], first)


if __name__ == "__main__":
    unittest.main()
