# opencode-subagentLink

An unofficial Claude Code plugin. Claude hands a task to one of your
[OpenCode](https://opencode.ai) V2 agents, keeps working, and is told when the
agent finishes.

## Quickstart

You need Claude Code 2.1.275 or later, OpenCode V2 with at least one provider
set up, and `python3` 3.10 or later on your PATH.

1. In Claude Code, install the plugin:

   ```text
   /plugin install opencode-subagentlink --marketplace Mushst/opencode-subagentLink
   ```

   On older Claude Code versions, run
   `/plugin marketplace add Mushst/opencode-subagentLink`, then
   `/plugin install opencode-subagentlink@opencode-subagentlink`.
2. Start a new Claude Code session and type `/opencode-subagentlink:delegate`.
3. Claude asks the setup questions: your consent to the terms below, and which
   OpenCode agent to use by default. After that, ask Claude to delegate a task.

## How it works

One delegated task, end to end:

1. Claude calls the plugin's `spawn` tool. The plugin connects to OpenCode
   (starting OpenCode's background service if needed), creates a session for
   your chosen agent, and sends the task.
2. `spawn` returns a command. Claude runs it with Bash in the background. The
   command polls OpenCode every 3 seconds with no model involved.
3. When the worker finishes, or needs an answer from you, the command exits.
   Claude Code wakes Claude with the output: the status and the worker's final
   reply. Tool calls and intermediate notes are left out. Claude calls `result`
   if it needs every reply.

Only sessions created by this plugin can be steered through it. Your own
OpenCode sessions are never touched.

## Where workers run

The `server` setting chooses the OpenCode server:

| Setting | Server | When Claude closes |
| --- | --- | --- |
| `service` (default) | OpenCode's own background service, shared with your OpenCode app and CLI. Started on demand. | Workers keep running. Nothing reports their completion, but a later conversation can `attach` to them. |
| A local URL, such as `http://127.0.0.1:4096` | A server you run yourself. Needs `password_file`. | Same as `service`. |
| `private` | A server that belongs to one Claude session. Stops after 15 idle minutes and restarts on the next call. | The server stops and running workers are interrupted. |

If you already run your own `opencode serve`, use its URL. Otherwise the plugin
starts OpenCode's background service as a second server next to yours.

## What setup asks

The plugin's `setup_status` tool returns a checklist, and Claude asks you each
open item:

- **OpenCode location**, only if it is not found automatically.
- **Password file**, only for a server URL. This is a file such as
  `~/.config/opencode/server.env` with an `OPENCODE_PASSWORD=` or
  `OPENCODE_SERVER_PASSWORD=` line, readable only by you. The plugin saves the
  file's path and never the password.
- **Consent.** Workers run as your OpenCode agents, with the permissions you set
  in OpenCode. They can edit files and are billed to your OpenCode providers.
  The plugin adds no sandbox and never approves an OpenCode permission request
  for you.
- **Default agent**, picked from the agents OpenCode reports for the project.

## Tools

| Tool | Purpose |
| --- | --- |
| `setup_status`, `configure` | Show and save setup |
| `agents` | List agents and the models they resolve to |
| `spawn` | Start a worker; returns the background command |
| `send` | Steer a running worker or queue a follow-up |
| `status`, `result` | Read progress, pending questions, or full replies |
| `wait` | Fallback when background Bash is unavailable (20 seconds per call) |
| `interrupt` | Stop the current run; file changes are not undone |
| `reply`, `permission` | Answer a worker's question or permission request, as you decide |
| `attach` | Continue a session from an earlier conversation |

## Settings and files

- Settings: `config.json` in the plugin's data folder
  (`~/.claude/plugins/data/<plugin id>/`). Claude Code keeps it across updates
  and deletes it when you uninstall the plugin.
- Private servers only: `runs/<id>.json` holds that server's address and
  password, mode 0600, deleted when the server stops. A startup log is kept only
  if startup fails.
- The plugin never edits OpenCode's configuration or credentials.

## Diagnostics

```sh
scripts/opencode-subagentlink doctor --home ~/.claude/plugins/data/<plugin id>
scripts/opencode-subagentlink doctor --probe --home ~/.claude/plugins/data/<plugin id>
```

The first command shows the setup checklist and starts nothing. `--probe` also
connects and lists agents; with the `service` setting it can start OpenCode's
background service.

## Limits

- Claude Code only. Claude Desktop chat and Cowork are not supported.
- Tested on Linux with OpenCode 2.2.1. macOS and Windows run the unit tests in CI
  but have not been tested with a real OpenCode server.
- Several servers on one OpenCode database at once (for example the built-in
  service and your own `opencode serve`) has not been tested.
- The server must be on the same machine, at `127.0.0.1` or `localhost`.

## Development

```sh
claude --plugin-dir .                       # load this checkout as a plugin
PYTHONPATH=src python -m unittest discover -s tests -v     # unit tests, with a fake OpenCode
PYTHONPATH=src BRIDGE_TEST_OPENCODE=$(command -v opencode) python -m unittest discover -s tests -p 'test_live.py'
claude plugin validate .
```

The live tests start real OpenCode servers with isolated home folders and no
provider credentials. They make no model calls. Design notes and the decision
record are in [docs/review-2026-10-29.md](docs/review-2026-10-29.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
