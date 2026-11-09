# project-plan: a live project board for Claude Code

A local web page that shows what Claude Code is doing in a project, lets you answer Claude without going back to the terminal, and keeps a record of every request. It runs on your computer: Python's standard library and a few hooks, with nothing to install from npm or pip. Nothing goes to the cloud unless you turn on phone alerts.

**What you get**

- **Live:** every Claude session in the project, with its plan, the current task, progress, context fill, cost (at API list prices), helper agents and failing checks. Click a changed file to see its `git diff`, and see which files aren't committed yet.
- **Answering Claude from the board:**
  - **Proposals.** When you ask Claude to brainstorm, suggest features or pick an approach, it posts a proposal with questions, an optional mockup and the plan it would follow. You answer, add instructions, and press **Approve and build**, **Ask for changes** or **Not now**.
  - **Approvals.** When a permission request comes in while the board is open, you can **Allow** or **Deny** it there.
  - **New requests.** Send Claude a new request (brainstorm, suggest features, do a task) from the board.
- **Record:** the **Today**, **Week**, **Timeline** and **History** views. History keeps every request with its tasks, files, checks, cost and Claude's reply, with search, ★ pins and your own notes. Today can be copied as a standup, and Week as a Markdown report.
- **All projects:** a hub at `http://localhost:4320` shows every project that has a board. You can answer approvals for all of them from that one tab, and it plays the alerts.
- **Alerts:** desktop notifications, a soft chime, a count in the tab icon, and phone alerts through [ntfy](https://ntfy.sh) when you're away from the board. You can also set a daily budget and a context limit, and Claude is told to wrap up when a limit is passed.
- **Comfort:** light and dark themes, keyboard shortcuts (`?` lists them), and a `⌘K` search box that jumps to anything.

## Requirements

- Claude Code (recent version), on macOS or Linux.
- `python3` 3.9 or later. It comes with macOS and most Linux distributions.
- `git` for the diff and uncommitted-file features.
- `gh`, signed in, for the daily update check. Optional: everything else works without it.
- Access to the `Codewing-Internal-Projects` GitHub organisation, because this is a private repository.

## Install (each team member, once)

```bash
gh auth login                     # if you haven't: Claude Code uses your git credentials to fetch private repos
claude plugin marketplace add Codewing-Internal-Projects/project-plan
claude plugin install project-plan@codewing
```

Restart Claude Code afterwards. You can do the same from inside Claude Code with `/plugin marketplace add Codewing-Internal-Projects/project-plan` and then `/plugin install project-plan@codewing`.

The plugin's hooks run in every project but do nothing unless the project has a board (a `.progress/` folder), so installing it changes nothing until you set up a board.

## Set up a board in a project

Open Claude Code in the project and say **"set up the progress board"**, or type `/project-plan`. Claude then:

1. creates `.progress/` in the project and adds it to `.gitignore`;
2. starts the board on a free port from 4321 upwards and gives you the link;
3. starts its **listener** in the background, which is how your answers on the board reach the session.

As a plugin, nothing else in the project changes: no edits to `.claude/settings.json` or `CLAUDE.md`. The plan-first rules reach Claude through the session start hook, so teammates who don't use the board see no difference in the repository.

Useful things to ask Claude:

| Ask | What happens |
|---|---|
| "open the board" | You get the link again (it restarts the server if needed). |
| "turn on phone access for the board" | The board becomes reachable on your Wi-Fi behind a private key, and you get the link. |
| "publish a snapshot of the board" | A read-only copy, which Claude can publish as a private claude.ai page. |
| "remove the board" | The project comes off the board and the hub. History stays in `.progress/` until you delete it. |

## How to work with it (team guidelines)

- **Let Claude plan first.** Claude writes a task list before starting work. The board times each task, and marks tasks added after work started as **added later**, so you can see how the plan changed.
- **Ask for proposals, not chat questions.** "Brainstorm…", "suggest features for…" and "which approach…" become proposals on the board. Answer them there. **Additional instructions** is the place for constraints ("no new dependencies", "keep the old API").
- **Keep the board or the hub open while Claude works** if you want to answer approvals from the browser. A permission request waits up to 40 seconds for an answer there before the terminal asks. With no board open, the terminal asks straight away, as usual.
- **Before you accept "done", check the check.** The board flags a test, lint or build that passed *before* the last edit, and the Today view marks flaky checks.
- **Cost is an estimate** at API list prices from the session's token counts. On a subscription it shows usage, not what you are billed.
- **Never commit `.progress/`.** It's local state: prompts, replies, file paths and your notes. Setup git-ignores it.
- **Privacy:**
  - **Phone alerts** go through ntfy.sh, and anyone who knows the topic can read them. Use **Make a private topic** in Settings.
  - **Phone access** lets anyone on your network who has the link (with its key) read the board and send requests to Claude.
  - **Snapshots** contain prompts and replies, so only publish them when you mean to share them.
- **Several projects:** each project gets its own board and port. Use the hub (`http://localhost:4320`, or **All projects ↗** on any board) to follow them all from one tab.

## Updating

Once a day, the session start hook asks GitHub (through `gh`) whether a newer version is published. If one is, Claude mentions it and the board shows a notice. To update:

```bash
claude plugin marketplace update codewing
claude plugin update project-plan@codewing
```

Then restart Claude Code. The boards and the hub restart with the new version on the next session start. To check right away, run `python3 ~/.claude/project-plan/hook.py check-update`.

## Moving from a hand-installed copy

If you used the earlier version copied into `~/.claude/skills/project-plan`:

1. Install the plugin (see above) and restart Claude Code.
2. In each project with a board, ask Claude to "set up the progress board" again. This removes the old hooks from `.claude/settings.json` and the old block from `CLAUDE.md`, and keeps the history.
3. Then delete `~/.claude/skills/project-plan`, so the skill isn't listed twice. If you delete it before step 2, the old project hooks fail with a "file not found" error until you rerun setup in that project.

## Uninstall

In each project, ask Claude to "remove the board", or delete the project's `.progress/` folder. Then:

```bash
claude plugin uninstall project-plan@codewing
```

To stop a board that is still running: `lsof -ti :<port> | xargs kill` (the hub uses port 4320).

## Troubleshooting

| Problem | Fix |
|---|---|
| The board is empty | Restart the Claude session, so the plugin's hooks load. Check that the project has `.progress/`. |
| Everything shows twice | An old hand install's hooks are still in `.claude/settings.json`. Ask Claude to set up the board again. |
| Allow / Deny buttons don't appear | The board or hub has to be open and visible when the request arrives. Otherwise answer in the terminal. |
| Your answer stays "Waiting for Claude" | The session's listener isn't running. Type anything in that session, or wait until Claude finishes its reply. |
| "The board server is out of date" | Start a new Claude session in the project, or ask Claude to set up the board again. |
| A test or build isn't detected as a check | Only commands that *start* with `npm`/`pnpm`/`yarn`/`bun` `[run] check\|test\|lint\|typecheck\|build\|e2e`, `vitest`, `tsc`, `eslint` or `playwright test` count (`CHECK_RE` in `hook.py`). |
| No update notices | Sign in to `gh` (`gh auth login`), with access to the organisation. |

## For maintainers

```
.claude-plugin/marketplace.json          the "codewing" marketplace (lists the plugin)
plugins/project-plan/
  .claude-plugin/plugin.json             plugin name and version (the version the update check compares)
  hooks/hooks.json                       the hooks; they run hook.py, which does nothing outside board projects
  skills/project-plan/SKILL.md           instructions Claude follows
  skills/project-plan/scripts/
    hook.py                              hooks, CLI, board server, hub server, update check
    setup.py                             set up or remove a board in a project; the rules text
    index.html                           the board page (copied into each project's .progress/)
    hub.html                             the all-projects hub page
```

**Try your changes** without installing them:

```bash
claude plugin validate .
claude --plugin-dir ./plugins/project-plan       # this session only
```

**Release a new version:**

1. Bump `version` in **both** `plugins/project-plan/.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`.
2. If the board or hub server changed, also bump `VERSION` in `hook.py`. Running servers restart when it changes.
3. Add a `CHANGELOG.md` entry.
4. Commit and push, then run `claude plugin tag --push`, which creates and pushes a `project-plan--v<version>` tag.

Team members are told about the new version within a day, at their next session start.
