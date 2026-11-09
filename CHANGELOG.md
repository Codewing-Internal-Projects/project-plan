# Changelog

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
