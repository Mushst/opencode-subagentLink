"""Per-user settings for this plugin. Never edits OpenCode's configuration or credentials."""

import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

NAME = "opencode-subagentlink"
HOME_ENV = "OPENCODE_SUBAGENTLINK_HOME"
PASSWORD_KEYS = ("OPENCODE_PASSWORD", "OPENCODE_SERVER_PASSWORD")
DEFAULT_IDLE_MINUTES = 15
SERVER_URL = re.compile(r"http://(?:127\.0\.0\.1|localhost):\d{1,5}/?")
TEXT_KEYS = ("opencode", "default_agent", "server", "password_file")


def home():
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / NAME


def config_path():
    return home() / "config.json"


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_server(value):
    """service = OpenCode's background service; private = a server per MCP connection; else a local URL."""
    if value in ("service", "private"):
        return value
    if isinstance(value, str) and SERVER_URL.fullmatch(value):
        return value.rstrip("/")
    raise ValueError("server must be 'service', 'private', or a local URL such as http://127.0.0.1:4096.")


def validate_idle(value):
    if type(value) not in (int, float) or not 1 <= value <= 1440:
        raise ValueError("idle_minutes must be a number from 1 to 1440.")
    return value


def load():
    path = config_path()
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError(f"Unsupported settings file: {path}")
    for key in TEXT_KEYS:
        if key in value and (not isinstance(value[key], str) or not value[key].strip()):
            raise ValueError(f"Invalid {key} in settings file: {path}")
    if "consent" in value and type(value["consent"]) is not bool:
        raise ValueError(f"Invalid consent in settings file: {path}")
    if "server" in value:
        validate_server(value["server"])
    if "idle_minutes" in value:
        validate_idle(value["idle_minutes"])
    return value


def server_mode(config):
    return config.get("server", "service")


def find_opencode(explicit=None):
    if explicit:
        candidate = shutil.which(explicit) or str(Path(explicit).expanduser().resolve())
        path = Path(candidate)
        if path.is_file() and (os.name == "nt" or os.access(path, os.X_OK)):
            return str(path.resolve())
        raise ValueError("OpenCode executable was not found; provide its executable path.")
    found = shutil.which("opencode")
    if found:
        return str(Path(found).resolve())
    names = ("opencode.exe", "opencode") if os.name == "nt" else ("opencode",)
    for directory in (Path.home() / ".opencode/bin", Path.home() / ".local/bin",
                      Path.home() / ".bun/bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")):
        for name in names:
            path = directory / name
            if path.is_file() and (os.name == "nt" or os.access(path, os.X_OK)):
                return str(path.resolve())
    raise ValueError("OpenCode is not installed or discoverable. Install OpenCode V2, "
                     "or supply its executable path.")


def read_password(path):
    """Read a server password from a bare-password file or KEY=VALUE lines. Never stored."""
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise ValueError("password_file must be an absolute path.")
    if os.name != "nt":
        info = path.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("password_file must be owned by you and private (chmod 600).")
    text = path.read_text(encoding="utf-8").strip()
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        if separator and key in PASSWORD_KEYS and value:
            return value
    if text and "=" not in text and "\n" not in text:
        return text
    raise ValueError("password_file has no OPENCODE_PASSWORD or OPENCODE_SERVER_PASSWORD entry.")


def configure(*, consent=None, opencode=None, default_agent=None, server=None,
              password_file=None, idle_minutes=None):
    current = load()
    if consent is not None:
        if consent is not True:
            raise ValueError("consent can only be set to true, after the user agrees.")
        current["consent"] = True
    if opencode is not None:
        current["opencode"] = find_opencode(opencode)
    if default_agent is not None:
        if not default_agent.strip():
            raise ValueError("Choose a nonempty agent ID.")
        current["default_agent"] = default_agent.strip()
    if server is not None:
        current["server"] = validate_server(server)
    if password_file is not None:
        read_password(password_file)  # Fail now, not at first use; only the path is saved.
        current["password_file"] = str(Path(password_file).expanduser())
    if idle_minutes is not None:
        current["idle_minutes"] = validate_idle(idle_minutes)
    current["schema_version"] = 1
    private_json(config_path(), current)
    return current


def require_setup():
    config = load()
    if not config.get("consent"):
        raise ValueError("Setup is not finished. Call setup_status and ask the user each missing item.")
    return config


def workspace_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("Workspace must be an absolute path on this machine.")
    path = path.resolve(strict=True)
    if not path.is_dir():
        raise ValueError("Workspace must be a directory.")
    return str(path)
