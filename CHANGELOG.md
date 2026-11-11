# Changelog

## 1.5.0 (2026-11-11)

- **Models:** session cards show the model the session runs on (e.g. *Opus 5.5*), following `/model` switches. Helper agents show theirs too: the requested model as soon as one starts, then the model it really runs on, read from its own transcript on its first tool call. History lists each agent's model.
- Fix: tasks marked in progress no longer spin when their session isn't working (waiting for you, quiet or ended); they show as *paused* with the time they ran.

## 1.4.0 (2026-11-10)

- **Security view:** a new view under Work for security-audit runs, replacing the audit box in the map's health check. It shows counts of confirmed findings, leads to check by hand, unreviewed areas and dismissed findings; an expandable card per finding (what it is, root cause, why it isn't confirmed, how to check it here and on a live site, the path through the code with links to the map, **Check/Fix with Claude**, **Not a problem**); and **Report**, **Coverage** and **Files** tabs that render every file the audit wrote (`REPORT.md`, `NEEDS-VALIDATION.md`, `findings.json` and the rest). A run picker switches between runs and marks findings that are new since the run before, or no longer reported. Unconfirmed leads (`needs_validation`) are shown too; before, only confirmed findings were.
- **Map health check:** the *Security audit* box now sums up the last run and links to the Security view; audit findings no longer mix with the pattern-matching issues.
- Fix: **Rescan** and **Not a problem** rebuild the map in a fresh process, so a board server started before an update no longer rebuilds it with old code (which dropped the security-audit summary).
- Keys `1` to `9` switch views (Security is `8`, Savings `9`).
- Internal: `hook.py` is split into `board_server.py` and `board_cli.py`, and `codemap.py` into extract, scan and health modules; no change in behaviour.

## 1.3.0 (2026-11-10)

- **Security audit on the map:** the Health panel has a *Security audit* box. **Run a security audit** sends the job to Claude, which uses Cloudflare's [security-audit](https://github.com/cloudflare/security-audit-skill) skill. The confirmed findings from the latest finished run then show up next to the health check issues, with **Show on map**, **Fix with Claude** and **Not a problem**. The box shows when the audit ran, how many findings still need checking by hand, and where `REPORT.md` is. Exports leave the audit summary out.
- **Savings view (TokenEconomizer):** what the [TokenEconomizer](https://github.com/microsoft/TokenEconomizer) plugin learned and saved in this repository: cost per request before and after it, learned facts, tool pruning (apply or restore from the board), the loop guard and its settings.
- **See what each request changed:** History records every edit, including files changed by shell commands (`sed -i`, heredocs, scripts). **View changes** shows a before/after diff, side by side or as the whole file.
- **Install guides for integrations:** when a tool the board works with isn't installed, its place on the board shows the install commands with **Copy** instead of a button that can't work. The README has a new *Works with other tools* section.
- **Tab titles:** browser tabs read *project-name | Project Board* (and *| Project Map*), so boards for different projects are easy to tell apart.
- Fix: TokenEconomizer tool names starting with `-` are rejected, so they can't be passed to `te` as options.

## 1.2.1 (2026-11-10)

- **Simpler install name:** the marketplace is now called `project-plan`, so it's `claude plugin install project-plan`, `claude plugin update project-plan` and `claude plugin marketplace update`, with no `@…` suffix. If you installed 1.2.0 or earlier, switch once (see Updating in the README).

## 1.2.0 (2026-11-10)

- **Interactive map:** a new page in the Map tab (and full-window at `/map.html`): pan and zoom, drill from the whole app into a part, a folder and a file, with **Used by / Inside / Uses** columns, a resizable details panel, search, right-click menus, keyboard shortcuts, filters for kinds of connections and technical detail, light and dark, and a phone layout with pinch and a bottom sheet.
- **Every project gets a map:** a free folder map at the first session; the scan reruns when sessions stop.
- **Scanner:** several repos in one folder; router and mount prefixes (Express, Fastify, Koa, Hono, Flask, FastAPI, Django, Laravel, Go, Axum, Actix); file-routed frameworks (Next.js, SvelteKit, Remix, Nuxt, Astro); NestJS, Spring, Firebase Functions, route tables; WordPress `admin_post_*`; who may call each endpoint; request helpers and API clients; **screens** for 10 front-end frameworks plus WordPress admin pages, shortcodes and blocks; unused files; descriptions follow renamed endpoints; `map path A B`.
- **Health check:** secrets in browser-exposed env vars, Supabase tables without row level security and open policies, Firebase rules, CORS with credentials, public reads, misplaced auth middleware, unused files; **Show on map**, **Fix with Claude**, **Not a problem** with a reason. Fewer false alarms (own guard helpers like `requireAdmin()`, table-name SQL, test files).
- **Ask:** answers come as an answer board with numbered steps, notes on each piece and follow-up questions; **Ask about this** on any card or connection; falls back to Codex when Claude Code isn't installed.
- **Flows and tour:** written by Claude with the map, or requested from the board in plain words and traced through the code.
- **Blueprints:** plan an app from a PRD (`map blueprint`, `map planned`), with a design review, **Change** with Undo and **Build this with Claude**; `map update --from-code` switches to the real code.
- **Recent work and live activity:** green parts and files for today's or this week's requests, a recent-work board, an "editing now" marker for running sessions, and **Impact** (what depends on a part or file).
- **Export and notes:** the map as one HTML file (flows, notes, recent work and health optional; no code, no folder paths); notes pinned to files.
- **Hub:** each project's map health and an **Open map** link.

## 1.1.0 (2026-11-10)

- **Map view:** the project as parts (screens, server, data, outside services, tools) with files, endpoints and connections. It comes from a free scan (imports, PHP class references, endpoints, requests matched to endpoints) plus a description Claude writes on request (`hook.py map worksheet` → `.progress/map/architecture.json` → `map described`). It rescans by itself when a session stops and flags parts whose files changed since they were described.
- **Recent work on the map:** parts touched by today's (or this week's) requests are highlighted; each part lists the requests that changed it, and Live and History cards show the parts a request touched.
- **Health check:** secrets in code, committed `.env` files, REST routes without a permission check, public endpoints that change data, SQL built from variables, unsafe input handling, `eval`, unverified Stripe webhooks, open CORS, very large files, localhost URLs. Each comes with **Ask Claude to fix** and **Dismiss**.
- **Ask the map:** questions about the code from the board, answered by `claude -p` with read-only tools, with file and line references and the parts involved highlighted.
- **Map lookups for Claude:** `hook.py map overview | find | show`, mentioned in the rules.
- **Blueprints:** proposals can include a planned map, drawn on the proposal with new and changed parts marked.

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
