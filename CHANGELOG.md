# Changelog

## 1.0.1 (2026-11-10)

- **Fix:** the History card header no longer gets pushed to the right edge by the ★ pin button.
- **README:** rewritten for first-time users, with screenshots of every view, diagrams of how the board and proposals work, a step-by-step setup, team guidelines, a glossary and an FAQ.

## 1.0.0 (2026-11-10)

First release as a Claude Code plugin in the `codewing` marketplace.

- **Board:**
  - **Live:** each session's plan, current task, context fill, cost, helper agents, flags and file diffs, with uncommitted files tagged and an **Ask Claude to commit** button.
  - **Proposals:** questions, mockups, additional instructions, and Approve / Ask for changes / Not now.
  - **New requests** from the board.
  - **Allow / Deny** for permission requests.
  - **Views:** Today (copy as standup), Week (copy as Markdown), Timeline and History (search, pins, notes).
- **Hub:** `http://localhost:4320` for all projects, with alerts and approvals in one tab.
- **Alerts:** desktop notifications, a chime, a tab badge, and ntfy phone alerts when you're away.
- **Limits:** a daily budget and a context limit, with a one-time nudge to Claude when one is passed.
- **Check history:** pass rate per check, with flaky checks flagged.
- **Session handling:** sessions can be renamed, titles are made from the first request, and sessions whose process has exited are marked ended. History can be pruned after N days.
- **Usability:** light and dark themes, keyboard shortcuts, the `⌘K` search box, phone access on the same Wi-Fi, and read-only snapshots.
- **Plugin mode:**
  - The hooks come from the plugin, and the rules reach Claude at session start, so projects get no edits to `settings.json` or `CLAUDE.md`.
  - A stable launcher sits at `~/.claude/project-plan/hook.py`.
  - A daily update check runs through `gh`.
