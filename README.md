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
- Python 3.10 or newer
- PyQt6
- A signed-in Claude desktop app with local credentials at
  `~/.claude/.credentials.json`

## Setup

From PowerShell, create an environment and install the app:

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -e .
```

For automatic startup, create a desktop shortcut and a Startup shortcut that
run `pythonw.exe` with `scripts\claude_code_usage_rings_watchdog.py` as the
argument and this repository as the working directory. The watchdog starts
the app in the background and restarts it after an unexpected exit.

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
```

## Interaction

- Drag the rings window with the left mouse button. Its position is saved and
  restored when it reappears.
- Click the window once, then use `Ctrl+-` to make it smaller or `Ctrl+=` /
  `Ctrl++` to make it larger.
- The app starts at the third-smallest size; press `Ctrl+-` once for the
  second-smallest setting and twice for the smallest.
- The two smallest settings hide the header and the `Weekly` / `remaining`
  ring labels. Their footer shows compact percentage-only values so every
  element stays separated.
- Press `Ctrl+T` to toggle the translucent glass background. The choice is
  saved for the next launch.
- The live rings icon stays in the Windows notification area instead of
  adding a taskbar button. Click it to show the window or right-click for
  controls.
- Hover over the window for full reset details when using the two smallest
  sizes.
- The header status badge shows `LIVE`, `SYNCING`, or `ERROR`, and appends
  the time of the last successful refresh (for example `LIVE · 14:32`) once
  usage data has loaded.

## Claude lifecycle detection

On Windows, the app checks the current Claude desktop process and the
packaged application frame. It polls twice per second so the rings follow
open, minimized, and closed Claude states without a second manual launch. If
Claude Code is running as a console/background process with no detectable
top-level window, the widget stays visible because the window state is
ambiguous rather than treating Claude as closed.

## Authentication and data

The app reads Claude Code's local credentials file:

```text
~/.claude/.credentials.json
```

It extracts the access token and account ID as needed, then requests usage
from a placeholder endpoint modeled on the Codex sibling project:

```http
GET https://api.anthropic.com/v1/usage
Authorization: Bearer <access_token>
anthropic-account-id: <account_id>
Accept: application/json
```

Anthropic does not currently publish a documented usage-percentage endpoint
for Claude Code the way OpenAI does for Codex, so the base URL, request
shape, and response parsing here are a structural placeholder carried over
from codex-widget — update `account_usage.py` once the real endpoint is
confirmed. Credentials stay local and are sent only with that usage request.

## Project structure

```text
scripts/launch_usage_rings.py                       Development launcher
scripts/claude_code_usage_rings_watchdog.py          Startup supervisor
src/claude_code_usage_rings/app.py                   Application entry point
src/claude_code_usage_rings/host_window.py           Claude process/window detection
src/claude_code_usage_rings/account_usage.py         Authentication and usage fetching
src/claude_code_usage_rings/models.py                Usage formatting
src/claude_code_usage_rings/rings_window.py           PyQt6 window and ring rendering
```
