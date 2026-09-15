# Claude Code Usage Rings

A standalone Windows desktop app that displays remaining Claude Code capacity
in two nested rings. The outer ring is the 5-hour limit, the inner ring is
the weekly limit, and each ring changes from green to amber to red as
capacity is used. It is a Claude-branded sibling of the
[codex-widget](https://github.com/your-account/codex-widget) project, built from
the same design.

The app follows the Claude desktop window: it appears when Claude is
available, hides when Claude is minimized or closed, restores its last
position, and keeps running across Claude restarts through a small watchdog
process.

## Requirements

- Windows 10 or 11
- Python 3.10 or newer on `PATH` (or available through the `py` launcher)
- Claude Code CLI signed in to a Claude.ai account

## Installation on another Windows system

Clone the public repository, create an isolated Python environment, and
install the widget:

```powershell
# Replace your-account with the GitHub owner of this repository.
git clone https://github.com/your-account/claude-code-widget.git
cd claude-code-widget
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -e .
```

If `python` is not available, replace it with `py -3.11` (or another
installed Python 3.10+ version). Sign in to Claude Code before launching:

If Claude Code is not installed yet, install the official native Windows
client first (see the [Claude Code setup guide](https://code.claude.com/docs/en/setup)):

```powershell
irm https://claude.ai/install.ps1 | iex
```

```powershell
claude auth login --claudeai
```

Confirm that the credentials file exists before starting the widget:

```powershell
Test-Path "$env:USERPROFILE\.claude\.credentials.json"
```

The widget reads the local credential at
`%USERPROFILE%\.claude\.credentials.json`; never copy that file into this
repository. If Claude Code uses a custom profile, set `CLAUDE_AUTH_FILE` or
the relevant Claude Code configuration directory before launching.

To verify the installation immediately:

```powershell
.venv\Scripts\python.exe scripts\launch_usage_rings.py --start-visible
```

For automatic startup, create a desktop shortcut and a Startup shortcut that
run `pythonw.exe` with `scripts\start_with_update.py` as the argument and this
repository as the working directory. It pulls the latest `main` from GitHub,
then hands off to the watchdog, which starts the app in the background and
restarts it after an unexpected exit. Update results are logged to
`%LOCALAPPDATA%\ClaudeCodeUsageRings\update.log`.

## Usage

Run the app directly while developing:

```powershell
python scripts\launch_usage_rings.py
```

Show it immediately for a manual check:

```powershell
python scripts\launch_usage_rings.py --start-visible
```

After installation, the equivalent command is:

```powershell
claude-code-usage-rings
```

Optional arguments:

```text
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
  dragging either card moves the connected pair together. Press `Ctrl+S` while
  a widget is focused to separate the pair.
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
  stays closed until the next sign-in or until you start it again.
- The widget stays off the Windows taskbar like the Codex sibling and keeps a
  live rings icon in the notification area. Click the tray icon or right-click
  it for controls.
- Hover over the window for full reset details when using the two smallest
  sizes.
- The header status badge shows `LIVE`, `SYNCING`, or `ERROR`, and appends
  the time of the last successful refresh (for example `LIVE · 14:32`) once
  usage data has loaded.
- Temporary Claude service throttles (`HTTP 429`) are retried with the
  server's `Retry-After` value when provided. If the widget already has data,
  it keeps the last successful rings visible and shows `SYNCING` until the
  next refresh succeeds. With no usage data yet, a failed refresh always shows
  `ERROR` with the reason instead of `SYNCING`, so the widget never looks busy
  when it has actually given up.
- Repeated failures back the refresh off exponentially, from the normal
  interval up to 15 minutes, and reset on the first success.
- Every refresh failure is written to
  `%LOCALAPPDATA%\ClaudeCodeUsageRings\usage.log` (override with
  `--log-file`).

## Troubleshooting

If the badge does not reach `LIVE`, run one refresh in the foreground:

```powershell
.venv\Scripts\python.exe scripts\launch_usage_rings.py --diagnose
```

It prints the credentials path, the raw usage response, and the parsed
windows, or the exact failure. `HTTP 401`/`HTTP 403` means the stored
credentials need `claude auth login --claudeai`; `HTTP 429` means Claude is
throttling the account and the widget will retry with backoff.

Note that the widget renews the OAuth token only when it is actually
expiring, and at most once every 10 minutes. Claude Code rotates the refresh
token on each renewal, so renewing on every tick both invites `HTTP 429` from
the token endpoint and can invalidate the credentials Claude Code itself is
using.

## Claude lifecycle detection

On Windows, the app checks the current Claude desktop process and the
packaged application frame. It polls twice per second so the rings follow
open, minimized, and closed Claude states without a second manual launch. If
Claude Code is running as a console/background process with no detectable
top-level window, the widget stays visible because the window state is
ambiguous rather than treating Claude as closed.

## Authentication and data

The app reads Claude Code's local OAuth credentials file:

```text
~/.claude/.credentials.json
```

It extracts the access token and account ID as needed, then requests usage
from a placeholder endpoint modeled on the Codex sibling project:

```http
GET https://api.anthropic.com/api/oauth/usage
Authorization: Bearer <access_token>
anthropic-beta: oauth-2025-04-20
Accept: application/json
```

The OAuth usage route is the same endpoint Claude Code uses for the 5-hour and
weekly subscription meters. Credentials stay local and are sent only with
that usage request; rotated refresh credentials are written back atomically.

## Privacy and repository hygiene

The repository contains source code and installation documentation only. Do
not commit auth files, API keys, tokens, logs, screenshots, or machine-specific
paths. Local credential and environment files are ignored by `.gitignore`.

## Project structure

```text
scripts/launch_usage_rings.py                       Development launcher
scripts/claude_code_usage_rings_watchdog.py          Startup supervisor
scripts/start_with_update.py                        Startup entry: update, then supervise
src/claude_code_usage_rings/app.py                   Application entry point
src/claude_code_usage_rings/host_window.py           Claude process/window detection
src/claude_code_usage_rings/account_usage.py         Authentication and usage fetching
src/claude_code_usage_rings/models.py                Usage formatting
src/claude_code_usage_rings/rings_window.py           PyQt6 window and ring rendering
```
