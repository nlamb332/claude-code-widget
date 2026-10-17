# Usage Rings

A standalone Windows desktop app that displays remaining Claude Code and Codex
capacity in two nested rings. The outer ring is the 5-hour limit, the inner
ring is the weekly limit, and each ring changes from green to amber to red as
capacity is used. You choose which widgets to run: Claude Code, Codex, or both.

Each widget follows its desktop app: it appears when the app is available,
hides when the app is minimized or closed, restores its last position, and
keeps running across restarts through a small watchdog process. Each widget
runs as its own process, so the two can be snapped together or used alone.

## Requirements

- Windows 10 or 11
- Python 3.10 or newer on `PATH` (or available through the `py` launcher)
- For the Claude widget: Claude Code signed in to a Claude.ai account
- For the Codex widget: the Codex desktop app (part of the ChatGPT app) signed in

## Installation

Clone the repository, create an isolated Python environment, and install the
app:

```powershell
git clone https://github.com/nlamb332/claude-code-widget.git
cd claude-code-widget
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -e .
```

If `python` is not available, replace it with `py -3.11` (or another
installed Python 3.10+ version).

Sign in to whichever services you want to show:

- **Claude Code.** If it is not installed yet, install the official native
  Windows client first (see the
  [Claude Code setup guide](https://code.claude.com/docs/en/setup)):

  ```powershell
  irm https://claude.ai/install.ps1 | iex
  claude auth login --claudeai
  ```

  The widget reads the credential at `%USERPROFILE%\.claude\.credentials.json`.
  If Claude Code uses a custom profile, set `CLAUDE_AUTH_FILE` or the relevant
  Claude Code configuration directory before launching.

- **Codex.** Install the ChatGPT desktop app, which includes Codex, from the
  [ChatGPT download page](https://chatgpt.com/download/), open Codex, and sign
  in (or run `codex login`). The widget reads the credential at
  `%USERPROFILE%\.codex\auth.json`.

Never copy either credential file into this repository.

## Setup

Run setup once after installing:

```powershell
.venv\Scripts\python.exe scripts\setup.py
```

It asks which widgets to enable (Claude Code, Codex, or both), checks that each
one is signed in using the same credential paths the widgets read at runtime,
and writes `%LOCALAPPDATA%\UsageRings\config.json`, for example:

```json
{ "providers": ["claude", "codex"] }
```

It then offers to create a Desktop shortcut and a Startup-folder shortcut named
**Usage Rings** (using `assets\usage-widgets.ico`), and to start the widgets
right away. Run it again any time to change the selection. `usage-rings setup`
does the same thing once the package is installed.

Setup also takes flags, so it can be scripted:

```text
--providers claude,codex    Widgets to enable (claude, codex, or all)
--no-shortcuts              Create neither shortcut
--desktop-shortcut / --no-desktop-shortcut
--startup-shortcut / --no-startup-shortcut
--start / --no-start        Start the widgets when setup finishes
--desktop-dir PATH          Create the Desktop shortcut here instead
--startup-dir PATH          Create the Startup shortcut here instead
--non-interactive           Never prompt
```

When it cannot prompt (input is redirected or `--non-interactive` is given),
`--providers` is required and every unanswered question defaults to "no".
Set `USAGE_RINGS_CONFIG_DIR` to keep the config somewhere other than
`%LOCALAPPDATA%\UsageRings`, for example when testing.

## Startup

Both shortcuts run `pythonw.exe scripts\start_with_update.py` from this
repository. That script pulls the latest `main` from GitHub, reads the config,
and starts one watchdog per enabled widget in the background. The watchdog
starts the app and restarts it after an unexpected exit. A widget whose
watchdog is already running is left alone, so clicking the shortcut twice is
harmless. If there is no config yet, it opens setup instead of guessing.
Update results are logged to `%LOCALAPPDATA%\UsageRings\update.log`.

To see what would start without starting anything:

```powershell
.venv\Scripts\python.exe scripts\start_with_update.py --no-update --dry-run
```

Shortcuts from the earlier separate Claude and Codex repositories still work if
their target scripts exist, but **Usage Rings** replaces them; delete the old
ones (including **Start Codex and Claude Usage Widgets**) once setup has
created the new shortcut.

## Usage

Run one widget directly while developing:

```powershell
python scripts\launch_usage_rings.py --provider claude
python scripts\launch_usage_rings.py --provider codex --start-visible
```

After installation, the equivalent command is:

```powershell
usage-rings --provider claude
```

Optional arguments:

```text
--provider claude|codex     Which service to show (required)
--auth-file PATH
--base-url URL
--refresh-seconds SECONDS
--start-visible
--log-file PATH
--diagnose
```

## Interaction

- Drag the rings window with the left mouse button. Its position is saved and
  restored when it reappears.
- The Claude and Codex widgets move freely during a drag. On mouse release,
  they snap together only when the final position is within 20 pixels and
  overlaps at least 75% along the alignment axis. The visible card borders
  touch when snapped. Left/right placements align their top or bottom edges;
  top/bottom placements align their left or right edges. Once snapped,
  dragging either card moves the connected pair together. Double-click a
  snapped widget to separate the pair (`Ctrl+S` also works while a widget is
  focused).
- Click the window once, then use `Ctrl+-` to make it smaller or `Ctrl+=` /
  `Ctrl++` to make it larger.
- The app starts at the third-smallest size; press `Ctrl+-` once for the
  second-smallest setting and twice for the smallest.
- The title/status banner stays visible at every size. At the two smallest
  settings the refresh time is omitted and the footer uses compact
  percentage-only values so every element stays separated.
- Press `Ctrl+T` to toggle the translucent glass background. The choice is
  saved for the next launch.
- Press `Ctrl+Q` while a widget is focused, or choose **Quit usage rings** from
  its tray menu, to close that widget. Quitting also stops its watchdog, so it
  stays closed until you start it again (for example with the **Usage Rings**
  shortcut). The other widget keeps running.
- Each widget stays off the Windows taskbar and keeps a live rings icon in the
  notification area. Click the tray icon or right-click it for controls.
- Hover over the window for full reset details when using the two smallest
  sizes.
- The header status badge shows `LIVE`, `SYNCING`, or `ERROR`, and appends
  the time of the last successful refresh (for example `LIVE · 14:32`) once
  usage data has loaded.
- Temporary service throttles (`HTTP 429`) are retried with the server's
  `Retry-After` value when provided. If the widget already has data, it keeps
  the last successful rings visible and shows `SYNCING` until the next refresh
  succeeds. With no usage data yet, a failed refresh always shows `ERROR` with
  the reason instead of `SYNCING`, so the widget never looks busy when it has
  actually given up.
- Repeated failures back the refresh off exponentially, from the normal
  interval up to 15 minutes, and reset on the first success.
- Every refresh failure is written to a per-widget log (override with
  `--log-file`):
  `%LOCALAPPDATA%\ClaudeCodeUsageRings\usage.log` for Claude and
  `%LOCALAPPDATA%\CodexUsageRings\usage.log` for Codex. The watchdog writes
  `watchdog.log` in the same folder.

## Troubleshooting

If a badge does not reach `LIVE`, run one refresh in the foreground for that
widget:

```powershell
.venv\Scripts\python.exe scripts\launch_usage_rings.py --provider claude --diagnose
.venv\Scripts\python.exe scripts\launch_usage_rings.py --provider codex --diagnose
```

It prints the credentials path, the raw usage response, and the parsed
windows, or the exact failure.

Losing the network is not treated as a sign-in problem: offline refreshes
retry every 30 seconds, keep the last rings on screen, and never trigger a
renewal.

A widget does not appear at startup: confirm it is enabled by looking at
`%LOCALAPPDATA%\UsageRings\config.json` (or re-run setup), check
`%LOCALAPPDATA%\UsageRings\update.log` and the widget's `watchdog.log`, and
remember the widget stays hidden until its desktop app is open and not
minimized.

### Claude

If Claude still rejects the sign-in after Claude Code has tried to renew it,
run `claude auth login --claudeai`; `HTTP 429` means Claude is throttling the
account and the widget will retry with backoff. "Credentials do not contain
claudeAiOauth.accessToken" means Claude Code is signed out.

The widget never renews the OAuth token or writes the credentials file itself,
because the token endpoint refuses renewals that do not come from Claude Code.
When a usage request is rejected, the widget runs Claude Code's local
`claude -p /usage` command (no model call, so no usage cost), which renews the
sign-in the way Claude Code normally would, then reads the file again. It asks
at most once every 10 minutes. The CLI is found on `PATH`, in the Claude
desktop app's bundled copy, or at `CLAUDE_USAGE_CLI`.

### Codex

If Codex still rejects the sign-in after it tried to renew it, run
`codex login`.

The widget only reads `auth.json`; it never renews or rewrites it. The Codex
app holds the same refresh token and OpenAI rotates it on every renewal, so a
renewal by the widget could sign Codex out. When a usage request is rejected,
the widget instead asks Codex's own app server for the account with a token
refresh (`codex app-server`, `account/read` with `refreshToken: true`), which
makes no model call, then reads the file again. It asks at most once every 10
minutes. The CLI is found on `PATH`, in the Codex app's install folder, or at
`CODEX_USAGE_CLI`.

## Lifecycle detection

On Windows, each widget checks its desktop app's processes and packaged
application frame. It polls twice per second so the rings follow open,
minimized, and closed states without a second manual launch.

- **Claude** matches `Claude.exe`. If Claude Code is running as a
  console/background process with no detectable top-level window, the widget
  stays visible because the window state is ambiguous rather than treating
  Claude as closed.
- **Codex** matches `ChatGPT.exe` (the current desktop shell) and `codex.exe`.
  A background `codex.exe` helper can outlive the shell, so a matching
  top-level window is required: closing Codex hides the rings and opening it
  brings them back.

With multiple virtual desktops, a widget shows only on the desktop that holds
its app's window and follows it when you switch. The widget is a tool window,
which Windows would otherwise show everywhere, so each poll also asks DWM
whether the app's windows are cloaked (how Windows hides windows that live on
another desktop) and hides the rings when they are.

## Authentication and data

Each widget reads the local credentials file its CLI maintains:

```text
~/.claude/.credentials.json     (Claude)
~/.codex/auth.json              (Codex)
```

and requests usage from the same routes those tools use for their meters:

```http
GET https://api.anthropic.com/api/oauth/usage            (Claude)
Authorization: Bearer <access_token>
anthropic-beta: oauth-2025-04-20

GET https://chatgpt.com/backend-api/wham/usage           (Codex)
Authorization: Bearer <access_token>
chatgpt-account-id: <account_id>
```

The endpoints may change without notice. Credentials stay local and are sent
only with the usage request for their own service. The widgets only read the
credential files; renewing them is left to Claude Code and Codex.

## Privacy and repository hygiene

The repository contains source code and installation documentation only. Do
not commit auth files, API keys, tokens, logs, screenshots, or machine-specific
paths. Local credential and environment files are ignored by `.gitignore`.

## Project structure

```text
scripts/setup.py                       First-run setup (also: usage-rings setup)
scripts/start_with_update.py           Startup entry: update, then supervise enabled widgets
scripts/usage_rings_watchdog.py        Per-provider supervisor (--provider)
scripts/launch_usage_rings.py          Development launcher
src/usage_rings/app.py                 Application entry point (--provider)
src/usage_rings/cli.py                 `usage-rings` command and `setup` subcommand
src/usage_rings/config.py              config.json location and loading
src/usage_rings/setup.py               Setup flow and shortcut creation
src/usage_rings/usage.py               Shared usage types, HTTP and renewal plumbing
src/usage_rings/providers/             One module per service (claude.py, codex.py)
src/usage_rings/host_window.py         Host app process/window detection
src/usage_rings/single_instance.py     Named-mutex helpers
src/usage_rings/models.py              Usage formatting
src/usage_rings/rings_window.py        PyQt6 window and ring rendering
src/usage_rings/window_snap.py         Cross-process window snapping
```

Adding another service means adding a module in `providers/` that fills in a
`Provider` (credentials path, usage fetch and parse, host window, mutex names)
and registering it in `providers/__init__.py`.
