"""Standalone Windows app for Claude Code and Codex usage rings."""

# A widget exits with this code on a deliberate quit, so its watchdog stops
# supervising instead of restarting it. Lives here so the watchdog can import
# it without loading Qt.
USER_QUIT_EXIT_CODE = 10
