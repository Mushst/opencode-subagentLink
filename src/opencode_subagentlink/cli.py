"""Command line: MCP hosting, setup, diagnostics, and a wait command that polls without a model."""

import argparse
import json
import os
import subprocess
import sys

from . import __version__, settings
from .api import Unreachable, brief, validate_id
from .mcp import serve, setup_status
from .runtime import Link, connect_private, service_client, url_client


def cli_client(run_id):
    if run_id:
        return connect_private(run_id)
    config = settings.require_setup()
    mode = settings.server_mode(config)
    if mode == "private":
        raise ValueError("Settings use a private server; pass the --run-id that spawn returned.")
    if mode == "service":
        return service_client(settings.find_opencode(config.get("opencode")))
    return url_client(mode, config.get("password_file"))


def doctor(args):
    result = {**setup_status(), "plugin_version": __version__, "python_executable": sys.executable}
    result["ok"] = result["ready"]
    if args.probe:
        workspace = settings.workspace_path(args.workspace or os.getcwd())
        link = Link()
        try:
            client = link.get(workspace, config=settings.load())
            result["probe"] = {"server": link.describe(), "agents": client.agents(workspace)}
        finally:
            link.close()
    return result


def parser():
    root = argparse.ArgumentParser(prog=settings.NAME, description="Delegate Claude Code tasks to OpenCode V2 agents.")
    root.add_argument("--version", action="version", version=__version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", help="Settings folder; the plugin passes its data folder")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("mcp", parents=[common], help="Serve MCP over stdio")
    setup = sub.add_parser("setup", parents=[common], help="Save settings; --consent records the user's agreement")
    setup.add_argument("--consent", action="store_true", help="The user accepts worker permissions, billing, and lifetime")
    setup.add_argument("--agent", help="Default agent ID, chosen from discovery")
    setup.add_argument("--opencode", help="OpenCode V2 executable")
    setup.add_argument("--server", help="service, private, or a local URL such as http://127.0.0.1:4096")
    setup.add_argument("--password-file", help="File holding the password for a server URL")
    setup.add_argument("--idle-minutes", type=float, help="Private server only: stop after this many idle minutes")
    diagnostic = sub.add_parser("doctor", parents=[common], help="Show setup state; --probe also connects and lists agents")
    diagnostic.add_argument("--probe", action="store_true", help="Connect (may start OpenCode's background service)")
    diagnostic.add_argument("--workspace", help="Project folder for --probe; defaults to the current folder")
    for name in ("wait", "result", "status", "interrupt"):
        command = sub.add_parser(name, parents=[common], help="Operate on a session this plugin created")
        command.add_argument("--session-id", required=True)
        command.add_argument("--run-id", help="Only for a private server")
        if name in ("wait", "result"):
            command.add_argument("--after", required=name == "wait")
        if name == "wait":
            command.add_argument("--timeout", type=int, help="Seconds; by default waits until the worker "
                                 "finishes or needs input")
            command.add_argument("--full", action="store_true", help="Print every reply, not the short outcome")
    return root


def main(argv=None):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    if args.home:
        os.environ[settings.HOME_ENV] = args.home
    try:
        if args.command == "mcp":
            serve()
            return 0
        if args.command == "setup":
            settings.configure(consent=True if args.consent else None, opencode=args.opencode,
                               default_agent=args.agent, server=args.server,
                               password_file=args.password_file, idle_minutes=args.idle_minutes)
            result = setup_status()
        elif args.command == "doctor":
            result = doctor(args)
        else:
            validate_id(args.session_id, "ses")
            if args.command == "wait" and args.timeout is not None and args.timeout < 1:
                raise ValueError("wait --timeout must be at least 1 second.")
            client = cli_client(args.run_id)
            if args.command == "wait":
                result = client.wait(args.session_id, args.after, args.timeout)
                if not args.full:
                    result = brief(result)
            elif args.command == "result":
                result = client.report(args.session_id, args.after)
            elif args.command == "status":
                result = client.state(args.session_id)
            else:
                result = client.interrupt(args.session_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1 if result.get("ok") is False else 0
    except (ValueError, RuntimeError, OSError, KeyError, subprocess.SubprocessError) as exc:
        hint = " Is the OpenCode server still running?" if isinstance(exc, Unreachable) else ""
        print(json.dumps({"error": str(exc) + hint}, ensure_ascii=False), file=sys.stderr)
        return 1
