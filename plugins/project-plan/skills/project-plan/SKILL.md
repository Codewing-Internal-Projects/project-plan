---
name: project-plan
description: Sets up a live project board for the current project, a local web page where the user watches every Claude Code session (plan, current task, progress, approvals needed, failed commands, checks, file conflicts, context fill, cost, helper agents, plan drift), answers Claude's proposals with a form (answers, additional instructions, Approve / Ask for changes), sends new requests (brainstorm, suggest features, do a task), and reviews Today, a Timeline and the full History of every request and what was done. Updated automatically by hooks. Use when the user types /project-plan, or asks to "set up the progress board", "track this project's tasks live", "show me what Claude is doing", "live task dashboard", "project plan board", "brainstorm on the board", "suggest features on the board", or wants to watch or steer sessions in a browser. Also use for "uninstall"/"remove", "open the board", "phone access"/"open the board on my phone", and "publish/share a snapshot of the board".
---

# project-plan

A board at `http://localhost:<port>` for one project. Hooks write `.progress/`; a small local server (stdlib Python, `hook.py serve`) serves the page and accepts the user's answers. Nothing runs in the cloud, and there are no dependencies beyond `python3`.

Everything lives in this skill folder (the base directory shown when the skill loads; below, `<skill>`) and is shared by all projects. It normally comes from the **project-plan plugin** (installed with `claude plugin install project-plan`), whose hooks track every project that has a `.progress/` folder and stay silent everywhere else.
- `scripts/setup.py` installs into or removes the board from a project.
- `scripts/hook.py` is the hook, the CLI Claude uses (plans, proposals, listener, phone, snapshot) and the server.
- `scripts/hub.html` is the all-projects hub (`hook.py hub`, port 4320).
- `scripts/codemap.py` is the project map's scanner, health check and lookups; `scripts/mapapp.py` its AI and sharing side (Ask, flows, blueprints, export, notes); `scripts/map.html` the map page (`hook.py map …`).
- `scripts/index.html` is the page. Each project gets a copy that refreshes on session start, so editing it here updates every project.

## Install (default)

1. Run from the project root, with `<skill>` replaced by this skill's folder:
   ```bash
   python3 "<skill>/scripts/setup.py" "$PWD"
   ```
   It is safe to rerun: it refreshes the page, keeps the port and history, and swaps an older board server for the current one. It:
   - creates `.progress/` (page, `port`, data) and adds `.progress/` to `.gitignore` in git repos;
   - starts the server on a free port from 4321 upwards (several projects can run boards at once), the all-projects hub on 4320, and writes the launcher `~/.claude/project-plan/hook.py` that every command below uses;
   - **as a plugin** writes nothing else into the project: the plugin's hooks do the tracking, and its session start hook gives you the rules. It also removes hooks and a `CLAUDE.md` block left by an older hand install;
   - **as a hand-installed skill** (no plugin) it merges its hooks into `.claude/settings.json`, keeping every other setting, and writes the rules into `CLAUDE.md` between `<!-- project-plan:start/end -->` markers.
2. Tell the user the URL it printed. Mention **New request** for sending Claude work from the board, **Settings** for alerts, sound, a daily budget and phone access, and that pressing `?` lists the keyboard shortcuts.
3. **Start the listener** in the background (see below). Hooks usually work in this session right away. If the board shows nothing after your next tool call, tell the user to restart the session or open `/hooks` once.
4. **Then follow the rules yourself.** If there's work in progress, post the plan now.

If the user asked for the board in a folder that isn't a project (their home folder or `/`), ask which project first: the script refuses those folders.

## Uninstall

```bash
python3 "<skill>/scripts/setup.py" "$PWD" --uninstall
```
This takes the project off the hub and removes this skill's hooks and `CLAUDE.md` block if a hand install wrote them, and keeps `.progress/` history. With the plugin, the board stops tracking the project once `.progress/` is deleted. Offer to delete `.progress/` too. To stop the server: `lsof -ti :<port> | xargs kill`.

## Open the board

Read the port from `.progress/port` and give the user `http://localhost:<port>`. Views: `#live`, `#today`, `#week`, `#timeline`, `#proposals`, `#history` (keys 1–6). If nothing is listening, run setup again: it restarts the server.

## Rules for Claude in a project with the board

The session start hook gives you these (a hand install also writes them into `CLAUDE.md`), and the hooks repeat them when needed.

H=`python3 ~/.claude/project-plan/hook.py`

- **Plan first.** Before working on any request that needs tools, write the whole plan with the task list tool. Give each task a short title and a one-line summary: with TodoWrite, `Title — summary`; with TaskCreate, `subject` plus `description`. The first plan is kept as "the original plan": tasks you add once work has started are shown as **added later**, and dropped ones are listed, so write the real plan up front.
- Keep exactly one task `in_progress`, mark tasks done as soon as they finish. The board times each task and draws them on the Timeline.
- **No task list tool?** `$H plan "Title — summary" ...`, `$H add "Title — summary"`, `$H task <n> in_progress|done|blocked` (1-based).
- **Keep the listener running.** `$H listen` with Bash `run_in_background`. It exits when the user answers a proposal or sends a request from the board, and prints what they sent; you are woken up with that output. Act on it, then start the listener again. If it isn't running, the Stop hook and the next prompt deliver the message instead. The session start hook reminds you.
- **Brainstorms, feature suggestions and "which approach?" go on the board as proposals**, not as questions in chat. Post one, tell the user in one line that it's on the board, and stop. Build only after they approve it, following their answers and **additional instructions**. If they ask for changes, revise it (`--revise <ID>`); if they dismiss it, don't build it.
- Before saying work is done, make sure the latest test/check/build ran after your last edit. The board flags a passing check that is older than the edits.
- Never hand-edit anything in `.progress/`: sessions and the server write it under a lock.

### Proposals

```bash
$H propose - <<'JSON'
{"title": "Short name", "kind": "feature|brainstorm|plan|design",
 "summary": "One or two sentences: what this is and why.",
 "body": "Markdown: the idea, how it would work, trade-offs.",
 "questions": [
   {"text": "Which layout?", "type": "one", "options": [{"label": "Tabs", "detail": "why", "recommended": true}, {"label": "Sidebar"}]},
   {"text": "Which of these should I build?", "type": "many", "options": [{"label": "Idea A", "detail": "one line · effort"}]},
   {"text": "Anything else I should know?", "type": "text"}],
 "tasks": ["Title — summary", "Title — summary"],
 "preview": "/path/to/mockup.html"}
JSON
$H propose new.json --revise p3      # new version of p3 after "Ask for changes"
$H proposal p3 building              # then: done | dismissed
```
- Everything but `title` is optional. Ask only questions whose answer changes what you build, and mark the option you recommend (it is preselected).
- **Suggest features:** one `"many"` question with 3–6 concrete ideas, each with a one-line detail and rough effort.
- **`preview`:** when a mockup says it better than words, write a self-contained HTML file (inline CSS/JS, no external assets) and pass its path. It is shown sandboxed in the proposal.
- `tasks` is the plan you'll follow if approved; the user sees it before approving.

The user's answer arrives as text: the decision (APPROVED / ASKED FOR CHANGES / DISMISSED), their answers per question, and their additional instructions, followed by what to do next.

## Phone access and sharing

- **On the same Wi-Fi:** `$H phone on` restarts the server on the network and prints a link with a private key (kept as a cookie after the first visit; requests without it are refused). Give the user the link and say that anyone on their network who has it can read the board and send requests. `$H phone off` closes it again. The board's **Phone** button shows the link on the computer.
- **Anywhere (read-only):** `$H snapshot` writes `.progress/snapshot.html`, the whole board with its data baked in and the forms disabled. To share it as a private claude.ai page, publish that file with the Artifact tool (it is already a complete page). It contains prompts, replies and file paths, so say so before publishing, and only publish when the user asks.

## What the board shows

- **Live:** totals (sessions working, needs you, tasks done, files changed, spend today against the budget); an alert strip for proposals waiting for input, approval requests (with **Allow / Deny**), shared files, failing checks, the daily budget and sessions past the context limit. Click a changed file in the side panel to see its `git diff`; files not committed yet are tagged, with **Ask Claude to commit** for a running session. ✎ renames a session (titles are otherwise made from its first request). Sessions whose Claude process has exited are marked ended automatically. Below that, a compact session table (status, current task, plan progress, context %, cost, flags) and a side panel with details: Now, context and cost, the plan with "added later" tags and an original-plan comparison, helper agents nested under the session (their status, current action, duration and cost), activity, last reply, files changed and earlier requests.
- **Today:** what shipped, what's still open, blockers, check history (pass rate, the last 20 runs, flaky checks flagged), files touched, spend; **Copy as standup**.
- **Week:** the last 7 days by day (shipped, requests, time on tasks, cost), everything shipped, the most changed files; **Copy as Markdown**.
- **Timeline:** one lane per session with request spans, task bars, agent bars and stalls, over 2 h / 8 h / 24 h / 7 d.
- **Proposals:** answer form with questions, **Additional instructions**, Approve and build / Ask for changes / Not now, versions, and requests sent from the board.
- **History:** every request (from the terminal or the board) with its tasks done, plan drift, files, check, cost, agents and Claude's reply, grouped by day and searchable. ★ pins a request and **Add note** keeps your own note on it; **Pinned only** turns History into a changelog.

### Approving from the board, budget and alerts

- **Allow / Deny on the board:** when a permission request arrives while a board tab is open and visible, the hook waits up to 40 s (`PERMIT_WAIT` in `hook.py`) for an answer there; the terminal asks only after that. With no board open it goes straight to the terminal, as before.
- **Settings → Limits:** a daily $ budget and a context limit (default 80%). Past one, the board flags it and the hooks tell Claude once (per day, per session) to wrap up.
- **Settings → Alerts:** desktop notifications and a soft chime; the tab icon shows how many things need you.
- **Settings → Phone alerts when you're away:** an ntfy topic (and server, default `https://ntfy.sh`). While no board tab is open, the hooks push “needs you”, “done” and new proposals there (`hook.py push`). Alert text leaves the machine, so suggest **Make a private topic**.
- **Settings → History:** keep history for N days; older requests and previews are pruned once a day. Empty keeps everything.
- **Keyboard:** `⌘K` / `Ctrl+K` go to anything (sessions, proposals, requests, files, actions), `1`–`7` views, `m` search the map, `j`/`k` next/previous session or proposal, `a`/`x` allow/deny the selected session's request, `n` new request, `/` search history, `t` theme, `?` help.

Cost is an estimate at Claude API list prices (from the transcript's token usage), not what a subscription bills. Context % is the last request's size against the model's window (1M on current models, 200K on older ones). Update `PRICES` in `hook.py` when prices change.

## The project map

The board's **Map** tab shows `map.html` (also full-window at `/map.html`): the project as parts (screens, server, data, outside services, tools) with files, endpoints, screens and connections, drawn from `.progress/map/graph.json`. Format and server API: `docs/map-format.md` in the plugin repo.

- **Scan (free, no tokens):** `$H map update`. It reads every file git knows about (several repos in one folder too) and extracts imports, PHP class references, endpoints with who may call them, requests matched to endpoints, screens, unused files and the health check. It runs at the first session of every project (folders-only map) and, detached, whenever a session stops.
- **Describe (Claude, only on request):** when the user asks to make or update the map, run `$H map worksheet` (or `map worksheet PART`, `--all`) and follow it: write `.progress/map/architecture.json` (parts with globs, links the scan can't see with a kind, one line per file and endpoint, 2–4 `flows`, a short `tour`), then `$H map described`. Developer style by default.
- **Lookups for any session:** `$H map overview`, `$H map find WORDS`, `$H map show PART|FILE|ENDPOINT|SCREEN`, `$H map path A B`. Use them before searching the code from scratch.
- **Ask / flows:** the board runs `claude -p` (Read, Grep, Glob only; Codex `exec --sandbox read-only` if Claude Code isn't installed) for answers and for flows requested in plain words. From the terminal: `$H map ask "…"`, `$H map flow "…"`. They cost tokens; the board shows the cost. The helper run sets `PROJECT_PLAN_OFF=1` so it isn't a session.
- **Blueprints:** to plan an app from a PRD or spec, run `$H map blueprint <PRD>` and follow it (writes `.progress/map/plan.json`), then `$H map planned`. The map shows dashed parts, a design review, **Build this with Claude**, and **Change** (reshape the plan from the board, with Undo). Once code exists, `$H map update --from-code`.
- **Export:** `$H map export [file]` or **Export** on the map: one HTML file, no code and no folder paths; flows and notes in, health and recent work only if chosen.
- **Board requests from the map:** **Fix with Claude**, **Build this with Claude** and **Update the map** arrive through the listener like any board request: do the work as asked.
- **Proposals:** a proposal can carry `"map": {"parts": [...], "links": [...]}` (status new/changed/existing/removed) and the board draws it as a small blueprint.

## All projects: the hub

`http://localhost:4320` lists every project with a board: what's running, what needs you, open tasks, shipped today, failing checks and spend, with the totals at the top. Projects with no activity for 7 days are folded away.
- Setup and every session start add the project to `~/.claude/project-plan/projects.json`, and start (or upgrade) the hub. `--uninstall` takes the project off.
- While the hub tab is open, project tabs stay quiet and the hub plays the alerts, desktop notifications and tab badge for every project. While it's visible, permission requests wait for **Allow / Deny** there too; it forwards them to the project's own server.
- The hub only reads each project's `.progress/` files. It's local only: no phone access, the same Host and Origin checks as the boards, and paths come only from the registry.
- Each project board's header links to it (**All projects ↗**).

## Updates

With the plugin, the session start hook checks GitHub once a day (through `gh`, so it uses the user's GitHub login) and, if a newer version is published, tells you; mention it to the user in one line. The board shows the same notice. To update: `claude plugin marketplace update`, then `claude plugin update project-plan`, and restart Claude Code. `$H check-update` checks right now.

## Troubleshooting

- **Everything shows twice:** an old hand install's hooks are still in the project's `.claude/settings.json` next to the plugin. Rerun setup; it removes them.
- **Hub doesn't list a project:** start a Claude session there (or rerun setup); boards set up earlier register on their next session start.
- **Board empty:** check `.claude/settings.json` has the hook entries (rerun setup), and that `.progress/progress.json` changes after a tool call.
- **Submit says the server is out of date:** rerun setup (or start a new session); it replaces the old server.
- **Allow / Deny buttons don't show:** the board only answers requests that arrive while it's open and visible; otherwise answer in the terminal.
- **Answer stays "Waiting for Claude":** the session's listener isn't running. Type anything in that session, or it's delivered when Claude next finishes a reply.
- **"No .progress/ folder here" from the CLI:** run it from inside the project, or install first.
- **A check isn't detected:** only commands that *start* with `pnpm|npm|yarn|bun [run] check|test|lint|typecheck|build|e2e`, `vitest`, `tsc`, `eslint` or `playwright test` count (`CHECK_RE` in `hook.py`).
