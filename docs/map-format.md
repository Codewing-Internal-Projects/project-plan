# Project map: data format and API

The contract between the scanner (`scripts/codemap.py`), the map page (`scripts/map.html`), the AI/sharing side (`scripts/mapapp.py`) and the board server (`scripts/hook.py`). Keep it in step with the code.

Everything lives in the project's `.progress/map/`. The board server serves `.progress/` as static files, so the page reads `map/graph.json` etc. directly.

| File | Written by | What |
|---|---|---|
| `architecture.json` | Claude (by hand, when asked) | parts, links, flows, tour, descriptions |
| `described.json` | `map described` | fingerprints at the last description (to flag stale parts) |
| `graph.json` | `map update` / `build()` | everything the page draws |
| `dismissed.json` | server | `{issue_id: {"at": iso, "reason": str}}` |
| `notes.json` | server | `{path: {"text": str, "at": iso}}` notes pinned to files |
| `asks.json` | server | last 20 answers `[{q, answer, nodes, followups, cost, at}]` |
| `flows.json` | server | flows requested from the board `[flow]` (merged into graph.flows) |
| `plan.json`, `plan.prev.json` | Claude / server | blueprint mode (see below) |

## Node ids (used everywhere: answers, flows, tour, health, search)

- `part:<part id>` (outside services are parts with `external: true`)
- `group:<part id>:<folder>`: a folder inside a part (the page computes groups from file paths)
- `file:<path>`
- `ep:<endpoint key>` where the key is `"<METHOD> <path>"`, e.g. `ep:POST /api/orders`
- `screen:<screen id>`

## graph.json

```jsonc
{
  "built": "iso", "scanned": "iso", "described": "iso or ''",
  "auto": false,               // true = no architecture.json: parts are folders
  "blueprint": false,          // true = drawn from plan.json, not code (see Blueprint)
  "project": "name", "root": "/abs/path (removed on export)", "repos": ["web", "api"],  // repos: [] for a single repo
  "style": "developer|plain", "summary": "…",
  "parts": [{
    "id": "orders-api", "name": "Orders", "kind": "screen|server|data|service|tool|other",
    "summary": "…", "external": false,
    "files": ["path", …], "endpoints": ["POST /api/orders", …], "lines": 1234,
    "new": [paths], "changed": [paths], "removed": [paths]      // since last description
  }],
  "edges": [{
    "from": "part id", "to": "part id",
    "imports": 3, "calls": 2,            // counted from code
    "label": "…", "kind": "uses|request|job|webhook|queue|event|data"  // from architecture links ('' if none)
  }],
  "files": {"path": {
    "part": "id|null", "repo": "name|''", "desc": "…", "lines": 120,
    "defs": ["exported names", …], "uses": [paths], "usedBy": [paths],
    "calls": ["endpoint keys"], "pkgs": ["npm/composer packages"], "hosts": ["api.stripe.com"],
    "unused": false, "config": false    // config: non-code file kept only because a part matches it
  }},
  "endpoints": {"POST /api/orders": {
    "method": "POST", "path": "/api/orders", "kind": "http|callable|ajax",
    "file": "path", "line": 12, "handler": "name or ''",
    "part": "id", "desc": "…", "callers": [paths],
    "auth": "public|signed-in|admin|unknown",  // public = callable by anyone
    "public": true, "nonce": false            // kept for compatibility
  }},
  "screens": [{
    "id": "checkout", "name": "Checkout", "route": "/checkout", "file": "path",
    "framework": "next|react-router|vue|sveltekit|remix|nuxt|astro|expo|angular|tanstack|wp-admin|wp-shortcode|wp-block",
    "files": [paths: the screen file and the components it imports, transitively, front-end only],
    "calls": ["endpoint keys it reaches"]
  }],
  "flows": [{"id": "checkout", "name": "Checkout", "summary": "…", "steps": [{"node": "node id", "text": "narration"}], "requested": false}],
  "tour": [{"node": "node id", "text": "…"}],
  "unassigned": [paths], "stale": [part ids],
  "health": {
    "high": 0, "medium": 0, "low": 0,     // open (not dismissed) counts
    "issues": [{
      "id": "rule:hash", "rule": "secret", "severity": "high|medium|low",
      "title": "…", "detail": "…", "fix": "prompt text for Claude",
      "locations": [{"file": "path", "line": 3, "text": "short note (never a secret value)", "node": "file:… or ep:…"}],
      "parts": [part ids], "dismissed": false, "reason": ""
    }]
  },
  "review": [ /* blueprint only: same shape as health issues, rule = design-review rule */ ],
  "plan": { /* blueprint only: {"source": "PRD path", "stack": [...], "assumptions": [...], "questions": [...]} */ }
}
```

## Recent work and live activity (the page reads the board's own files)

- `history.jsonl`: one JSON per line, last line per `id` wins: `{id, session, title, prompt, started, ended, done: [{title, summary}], files: [{path, add, del}]}`.
- `progress.json`: `{sessions: {id: {title, status: "working|active|idle|quiet|ended", updated, files: [{path, add, del, at}], round: {prompt}}}}`. Sessions whose status is working/active/idle are live.

## Server API (POST = JSON body, same-origin; all under the board's port)

| Route | Body → Response |
|---|---|
| `POST api/map/rescan` | `{}` → `{ok}` |
| `POST api/map/dismiss` | `{id, on: bool, reason}` → `{ok}` |
| `POST api/map/note` | `{path, text}` (empty text deletes) → `{ok}` |
| `POST api/map/ask` | `{q, context: [node ids], followup: bool, mode: "ask"\|"change"}` → ask: `{answer: markdown, nodes: [{id, note, step: n\|null}], followups: [str], cost, at, by}`; change (blueprint): `{summary, changed: [node ids], followups, cost, by}`. `by` names the agent that answered (shown next to the cost). Old asks.json rows `{q, a, parts}` are still read. |
| `POST api/map/flow` | `{q}` → `{flow}` (saved to flows.json, appears in graph.flows) |
| `POST api/map/undo` | `{}` → `{ok}` (blueprint: restore plan.prev.json) |
| `POST api/request` | `{kind: "task", text}` → `{ok}`: hands work to a listening Claude session (Fix with Claude, Build this, Update the map) |
| `GET api/map/export?flows=1&notes=1&recent=0&health=0` | → one self-contained HTML file (attachment) |
| Any POST failing | non-2xx with `{error: "message"}`: the page shows the message as is |
| `GET api/ping` | `{app, v, …}`: present only when served (not in an exported file) |

`window.MAPDATA` is set in an exported file: `{graph, notes, history?, exported: "iso"}`. When it is set the page is read-only: no Ask bar, no Fix/Build/Update buttons, no editor links, no folder path.

## Editor links

`vscode://file/<graph.root>/<path>:<line>`.

## Blueprint (plan.json)

Claude writes `plan.json` from a PRD (`map blueprint <PRD>` prints the instructions):

```jsonc
{"source": "docs/prd.md", "summary": "…", "stack": ["Next.js", "Postgres"], "assumptions": ["…"], "questions": ["…"],
 "parts": [{"id", "name", "kind", "summary", "files": [{"path", "desc"}]}],
 "endpoints": [{"method", "path", "part", "file", "desc", "auth": "public|signed-in|admin|service|undecided", "changes_data": bool}],
 "screens": [{"id", "name", "route", "part", "calls": ["METHOD /path"]}],
 "links": [{"from", "to", "label", "kind"}], "flows": [...], "tour": [...]}
```

`map planned` turns it into graph.json with `blueprint: true`, every part/file/endpoint as planned, and `review` (design review) instead of health.
