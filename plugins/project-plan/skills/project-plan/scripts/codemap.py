"""The project map: how the code fits together, for the board's Map view and for Claude.

A free scan (no tokens) reads every file git knows about and pulls out imports, class references,
server endpoints (with their full mounted paths and who may call them), the requests the code makes,
the screens a user can open, and a fingerprint per file. Claude writes .progress/map/architecture.json
once (the parts of the app, flows and a tour, in its own words); `build` then assigns files to parts,
connects them, runs the health check and writes .progress/map/graph.json.

    map update             rescan and rebuild (free; also runs when a session stops)
    map worksheet [PART]   what Claude reads to write or refresh architecture.json
    map described          after editing architecture.json: check it, remember what was described, rebuild
    map overview           the parts and how they connect
    map find WORDS         parts, screens, files and endpoints matching the words
    map show THING         one part, screen, file or endpoint in detail
    map path A B           the shortest chain of uses and calls from A to B (parts, files, endpoints, screens)
    map ask "QUESTION"     answer a question about the code (runs claude -p, read-only)
"""
import glob
import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone

ROOT = MAP = None
H = "python3 ~/.claude/project-plan/hook.py"
MAX_FILES = 6000
MAX_BYTES = 400_000
CODE = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".astro", ".php", ".py", ".go", ".rb", ".rs",
        ".java", ".kt", ".cs", ".swift"}
STYLE = {".css", ".scss", ".sass", ".less", ".html", ".twig", ".blade.php"}
TEXT = CODE | STYLE | {".json", ".yml", ".yaml", ".toml", ".ini", ".env", ".xml", ".sh", ".md", ".mdx", ".txt", ".sql", ".conf",
                       ".properties", ".rules"}
SKIP_DIRS = ("node_modules/", "vendor/", "build/", "dist/", ".next/", ".nuxt/", "out/", "coverage/", ".git/", ".progress/",
             "__pycache__/", ".venv/", "venv/", "target/", ".svelte-kit/", ".turbo/", ".cache/", ".output/", ".expo/")
SKIP_FILES = re.compile(r"(\.min\.(js|css)|\.map|\.lock|(?:^|[/._-])lock\.json|(?:^|[/._-])lock\.yaml|\.pot|\.po|\.mo|\.snap)$")
JS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".astro"}
KINDS = ("screen", "server", "data", "service", "tool", "other")
LINK_KINDS = ("uses", "request", "job", "webhook", "queue", "event", "data")
TEST = re.compile(r"(^|/)(tests?|__tests__|spec|specs|e2e|fixtures?|__mocks__|mocks?|stories)/|\.(test|spec|stories|e2e)\.\w+$|"
                  r"(^|/)test_[^/]*\.py$|_test\.(py|go)$|(^|/)conftest\.py$")
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE", "ANY", "AJAX", "CALL")


def setup(root, progress_dir):
    global ROOT, MAP
    ROOT, MAP = root, os.path.join(progress_dir, "map")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rj(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def wj(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)


def ext(path):
    return ".blade.php" if path.endswith(".blade.php") else os.path.splitext(path)[1].lower()


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ---------------------------------------------------------------- listing

def git(*args, cwd=None):
    try:
        r = subprocess.run(["git", "-C", cwd or ROOT, *args], capture_output=True, timeout=30)
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def find_repos():
    """A folder holding several git repos (web/, api/, apps/*): their folder names. [] for a normal single repo."""
    if git("rev-parse", "--is-inside-work-tree") is not None:
        return []
    cands = [d for pat in ("*", "apps/*", "packages/*", "services/*") for d in glob.glob(os.path.join(ROOT, pat))
             if os.path.isdir(d) and os.path.exists(os.path.join(d, ".git"))]
    out, seen = [], set()
    for d in sorted(set(cands), key=lambda d: (os.path.isfile(os.path.join(d, ".git")), d)):  # real repos before worktrees
        common = (git("rev-parse", "--git-common-dir", cwd=d) or "").strip()
        common = os.path.realpath(os.path.join(d, common)) if common else d
        if common in seen:  # another worktree of a repo already listed
            continue
        seen.add(common)
        out.append(os.path.relpath(d, ROOT))
    return out


def list_files(repos=()):
    """Every file git knows about (tracked, or new and not ignored); a plain walk outside git. Paths are prefixed
    with the repo folder when the project holds several repos."""
    paths = []
    for sub in (repos or [""]):
        cwd = os.path.join(ROOT, sub) if sub else ROOT
        out = git("ls-files", "-z", "-co", "--exclude-standard", cwd=cwd)
        if out is not None:
            got = [p for p in out.split("\0") if p]
        else:
            got = []
            for d, dirs, names in os.walk(cwd):
                r = os.path.relpath(d, cwd)
                dirs[:] = [x for x in dirs if not (x.startswith(".") or (x + "/") in SKIP_DIRS)]
                got += [os.path.normpath(os.path.join(r, n)) for n in names]
        paths += [(sub + "/" if sub else "") + p for p in got]
    keep = []
    for p in sorted(set(paths)):
        if any(("/" + p).find("/" + s) >= 0 for s in SKIP_DIRS) or SKIP_FILES.search(p) or ext(p) not in TEXT and not os.path.basename(p).startswith(".env"):
            continue
        keep.append(p)
    return keep[:MAX_FILES]


def tracked(repos=()):
    out = set()
    for sub in (repos or [""]):
        got = git("ls-files", "-z", cwd=os.path.join(ROOT, sub) if sub else ROOT)
        out |= {(sub + "/" if sub else "") + p for p in (got or "").split("\0") if p}
    return out


# The scanner lives in sibling modules; they read ROOT/MAP from here at call time, so import codemap first.
from codemap_scan import scan
import secaudit
from codemap_health import health


# ---------------------------------------------------------------- build

def glob_rx(g):
    g = g.strip().lstrip("./")
    if not any(c in g for c in "*?[") and (g.endswith("/") or "." not in os.path.basename(g)):
        g = g.rstrip("/") + "/**"
    out, i = "", 0
    while i < len(g):
        if g[i:i + 3] == "**/":
            out += "(?:.*/)?"
            i += 3
        elif g[i:i + 2] == "**":
            out += ".*"
            i += 2
        elif g[i] == "*":
            out += "[^/]*"
            i += 1
        elif g[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(g[i])
            i += 1
    return re.compile(out + "$"), len(re.split(r"[*?\[]", g)[0])


def auto_parts(paths):
    """No architecture.json yet: one part per top folder (split the biggest one level down)."""
    top = {}
    for p in paths:
        top.setdefault(p.split("/")[0] if "/" in p else ".", []).append(p)
    parts = []
    for d, ps in sorted(top.items()):
        subs = {}
        for p in ps:
            bits = p.split("/")
            subs.setdefault("/".join(bits[:2]) if len(bits) > 2 else d, []).append(p)
        if len(ps) > 0.4 * len(paths) and len(subs) > 1 and len(paths) > 30:
            for s in sorted(subs):
                parts.append({"id": s.replace("/", "-"), "name": s + ("/" if s != d else ""), "kind": "other", "paths": [s + "/*" if s == d else s + "/**"]})
        else:
            parts.append({"id": d if d != "." else "root", "name": d + "/" if d != "." else "Top-level files", "kind": "other", "paths": [d + "/**"] if d != "." else ["*"]})
    return parts


def node_ok(node, g):
    kind, _, rest = str(node).partition(":")
    if kind == "part":
        return any(p["id"] == rest for p in g["parts"])
    if kind == "group":
        return any(p["id"] == rest.split(":")[0] for p in g["parts"])
    if kind == "file":
        return rest in g["files"]
    if kind == "ep":
        return rest in g["endpoints"]
    if kind == "screen":
        return any(s["id"] == rest for s in g["screens"])
    return False


def clean_flows(flows, g, requested=False):
    """Flows with their dead steps dropped; returns (flows, [(flow id, node)] dropped)."""
    out, dropped = [], []
    for fl in flows or []:
        if not isinstance(fl, dict) or not fl.get("id"):
            continue
        steps = []
        for s in fl.get("steps") or []:
            if isinstance(s, dict) and node_ok(s.get("node"), g):
                steps.append({"node": s["node"], "text": str(s.get("text", ""))})
            else:
                dropped.append((fl["id"], s.get("node") if isinstance(s, dict) else s))
        out.append({"id": str(fl["id"]), "name": str(fl.get("name") or fl["id"]), "summary": str(fl.get("summary", "")), "steps": steps,
                    "requested": bool(fl.get("requested", requested))})
    return out, dropped


def build(sc=None, quiet=False):
    sc = sc or scan()
    arch = rj(os.path.join(MAP, "architecture.json"), None)
    described = rj(os.path.join(MAP, "described.json"), {})
    dismissed = rj(os.path.join(MAP, "dismissed.json"), {})
    prev = rj(os.path.join(MAP, "graph.json"), {}) or {}
    files, eps = sc["files"], sc["endpoints"]
    auto = not arch or not arch.get("parts")
    parts_in = auto_parts([p for p, f in files.items() if not f.get("config")]) if auto else arch["parts"]
    fdesc, edesc = ({}, {}) if auto else (arch.get("files") or {}, arch.get("endpoints") or {})

    # Carry descriptions over to files that moved (same content, new path).
    by_hash = {v.get("hash"): k for k, v in described.items() if k not in files}
    moved = {}
    for p, f in files.items():
        if p not in fdesc and f.get("hash") in by_hash and by_hash[f["hash"]] in fdesc:
            moved[p] = by_hash[f["hash"]]

    rx = [(part["id"], *glob_rx(g)) for part in parts_in for g in (part.get("paths") or [])]
    owner = {}
    for p in files:
        hits = [(spec, pid) for pid, r, spec in rx if r.match(p)]
        if hits:
            owner[p] = max(hits)[1]
    parts = {}
    for part in parts_in:
        pid = str(part.get("id", "")).strip()
        if not pid:
            continue
        parts[pid] = {"id": pid, "name": part.get("name") or pid, "kind": part.get("kind") if part.get("kind") in KINDS else "other",
                      "summary": part.get("summary", ""), "external": bool(part.get("external")) or part.get("kind") == "service" and not part.get("paths"),
                      "files": [], "endpoints": [], "lines": 0, "new": [], "changed": [], "removed": []}
    for p, pid in owner.items():
        if pid in parts:
            parts[pid]["files"].append(p)
            parts[pid]["lines"] += files[p].get("lines", 0)
    # Endpoint descriptions follow a renamed route when its file and handler stay the same.
    prev_eps = prev.get("endpoints") or {}
    gone = {}
    for k, v in prev_eps.items():
        d = edesc.get(k) or v.get("desc")
        if d and k not in eps and v.get("handler"):
            gone.setdefault((v.get("file"), v["handler"], v.get("method")), set()).add(d)
            gone.setdefault((v.get("file"), v["handler"], ""), set()).add(d)
    for k, v in eps.items():
        pid = owner.get(v["file"])
        v["part"] = pid
        c = gone.get((v["file"], v.get("handler"), v["method"])) or gone.get((v["file"], v.get("handler"), "")) if v.get("handler") else None
        v["desc"] = edesc.get(k) or prev_eps.get(k, {}).get("desc") or (next(iter(c)) if c and len(c) == 1 else "")
        v["callers"] = []
        if pid in parts:
            parts[pid]["endpoints"].append(k)

    # What changed since Claude last described the map.
    if not auto:
        for p, f in files.items():
            pid = owner.get(p)
            if pid not in parts or f.get("config"):
                continue
            old = described.get(p)
            if p in moved:
                continue
            if not old:
                parts[pid]["new"].append(p)
            elif old.get("sig") != f.get("sig"):
                parts[pid]["changed"].append(p)
        for p, old in described.items():
            if p not in files and p not in moved.values() and old.get("part") in parts:
                parts[old["part"]]["removed"].append(p)

    # Connections: imports, requests to endpoints, and the links Claude added.
    edges = {}

    def edge(a, b, kind, why=""):
        if a and b and a != b and a in parts and b in parts:
            e = edges.setdefault((a, b), {"from": a, "to": b, "imports": 0, "calls": 0, "label": "", "kind": ""})
            if kind == "label":
                e["label"], e["kind"] = why
            else:
                e[kind] += 1
    used_by = {}
    for p, f in files.items():
        for u in f.get("uses", []):
            used_by.setdefault(u, []).append(p)
            edge(owner.get(p), owner.get(u), "imports")
        for c in f.get("calls", []):
            if c["to"] in eps:
                eps[c["to"]]["callers"].append(p)
                edge(owner.get(p), eps[c["to"]]["part"], "calls")
    for l in ([] if auto else arch.get("links") or []):
        if isinstance(l, dict):
            edge(str(l.get("from")), str(l.get("to")), "label", (str(l.get("label", "")), l.get("kind") if l.get("kind") in LINK_KINDS else "uses"))

    out_files = {}
    for p, f in files.items():
        if f.get("config") and p not in owner:
            continue
        out_files[p] = {"part": owner.get(p), "repo": f.get("repo", ""), "desc": fdesc.get(p) or fdesc.get(moved.get(p), ""), "lines": f.get("lines", 0),
                        "defs": (f.get("defs") or [])[:30], "uses": f.get("uses", []), "usedBy": sorted(used_by.get(p, [])),
                        "calls": [c["to"] for c in f.get("calls", [])], "pkgs": sorted(set(f.get("pkgs", [])))[:20],
                        "hosts": f.get("hosts", []), "unused": bool(f.get("unused")), "config": bool(f.get("config"))}
    issues = health(sc, tracked(sc.get("repos") or []))
    for i in issues:
        d = dismissed.get(i["id"])
        i["dismissed"] = bool(d)
        i["reason"] = d.get("reason", "") if isinstance(d, dict) else ""
        i["parts"] = sorted({owner.get(l["file"]) for l in i["locations"] if owner.get(l["file"])})
    for v in eps.values():
        v.pop("checked", None)
    stale = [p["id"] for p in parts.values() if p["new"] or p["changed"] or p["removed"]]
    unassigned = sorted(p for p in files if p not in owner and not files[p].get("config"))
    graph = {"built": now(), "scanned": sc["scanned"], "auto": auto, "blueprint": False, "project": os.path.basename(os.path.abspath(ROOT)),
             "root": os.path.abspath(ROOT), "repos": sc.get("repos") or [],
             "style": (arch or {}).get("style", "developer"), "summary": (arch or {}).get("summary", ""),
             "described": (arch or {}).get("described", ""),
             "parts": list(parts.values()), "edges": list(edges.values()), "files": out_files, "endpoints": eps, "screens": sc.get("screens") or [],
             "flows": [], "tour": [], "unassigned": unassigned, "stale": stale,
             "health": {"issues": issues, "high": sum(1 for i in issues if i["severity"] == "high" and not i["dismissed"]),
                        "medium": sum(1 for i in issues if i["severity"] == "medium" and not i["dismissed"]),
                        "low": sum(1 for i in issues if i["severity"] == "low" and not i["dismissed"])}, "audit": secaudit.summary(ROOT), "audit_skill": secaudit.skill_installed(ROOT)}
    flows, dropped = clean_flows((arch or {}).get("flows"), graph)
    asked, dropped2 = clean_flows(rj(os.path.join(MAP, "flows.json"), []), graph, requested=True)
    ids = {fl["id"] for fl in flows}
    graph["flows"] = flows + [fl for fl in asked if fl["id"] not in ids]
    tour, dropped3 = clean_flows([{"id": "tour", "steps": (arch or {}).get("tour") or []}], graph)
    graph["tour"] = tour[0]["steps"] if tour else []
    graph["dropped"] = dropped + dropped2 + dropped3
    wj(os.path.join(MAP, "graph.json"), {k: v for k, v in graph.items() if k != "dropped"})
    if not quiet:
        print(summary(graph))
    return graph


def summary(g):
    nf = sum(1 for f in g["files"].values() if not f.get("config"))
    h = g["health"]
    lines = [f"Map of {g['project']}: {len(g['parts'])} parts, {nf} code files, {len(g['endpoints'])} endpoints, {len(g.get('screens') or [])} screens, "
             f"{len(g['edges'])} connections{', ' + str(len(g['repos'])) + ' repos' if g.get('repos') else ''}."]
    if g["auto"]:
        lines.append(f"No architecture.json yet: parts are just folders. To make the real map, run `{H} map worksheet` and follow it.")
    else:
        if g["stale"]:
            lines.append(f"Needs describing: {', '.join(g['stale'])} (new, changed or removed files). Run `{H} map worksheet` when the user asks to update the map.")
        if g["unassigned"]:
            lines.append(f"{len(g['unassigned'])} files belong to no part: {', '.join(g['unassigned'][:8])}{' …' if len(g['unassigned']) > 8 else ''}")
    lines.append(f"Health: {h['high']} high, {h['medium']} medium, {h['low']} low.")
    return "\n".join(lines)


# ---------------------------------------------------------------- Claude's side

SCHEMA = """{
  "style": "developer",
  "summary": "Two or three sentences: what this project is and how it's put together.",
  "parts": [
    {"id": "rest-api", "name": "REST API", "kind": "server",
     "summary": "One line, developer style: what this part does and the main classes or modules.",
     "paths": ["includes/class-rest-*.php"]},
    {"id": "mailchimp", "name": "Mailchimp", "kind": "service", "summary": "Audience sync for finished quizzes."}
  ],
  "links": [{"from": "notifications", "to": "mailchimp", "label": "POST /3.0/lists/{id}/members", "kind": "request"}],
  "files": {"includes/class-rest-responses.php": "POST /responses: validates, rescoring server-side, stores the row."},
  "endpoints": {"POST /ml-quiz/v1/responses": "Public quiz submission; nonce + per-IP rate limit; recomputes the score."},
  "flows": [{"id": "take-quiz", "name": "Taking a quiz", "summary": "A visitor answers, submits, and gets a result.",
             "steps": [{"node": "screen:ml-quiz", "text": "The shortcode renders the quiz app."},
                       {"node": "ep:POST /ml-quiz/v1/responses", "text": "Answers are posted and scored on the server."},
                       {"node": "part:mailchimp", "text": "The finished response is synced to Mailchimp."}]}],
  "tour": [{"node": "part:rest-api", "text": "Start here: every screen talks to the server through these routes."}]
}"""

RULES = """How to write .progress/map/architecture.json (style: {style}):
- 4 to 12 parts. A part is something a developer would name when explaining the app: a screen or UI area, an API, a domain module (scoring, billing), storage, a job runner, tooling. Don't mirror folders unless the folders are the real boundaries.
- kind: screen (UI the user sees), server (handlers, business logic), data (tables, models, storage), service (outside services: payment, email, CRM; no paths), tool (build, tests, scripts), other.
- paths: globs relative to the project root. `**` crosses folders, `*` doesn't, a folder name means everything under it. The most specific pattern wins, so broad catch-alls are fine. Every code file should land in a part; tests and build tooling can share a "tool" part.
- links: only what the scan can't see (it already follows imports, PHP class references and request → endpoint matches): outside services, webhooks, cron, queues, events. Label with the call or event; kind is one of uses, request, job, webhook, queue, event, data.
- files: one line per code file, {style_hint}. endpoints: one line per endpoint key as listed, saying who may call it.
- flows: 2 to 4 journeys through the app (a user action from screen to storage and outside services). Each step names a node: part:<id>, screen:<id>, ep:<METHOD path>, file:<path>; text says what happens there.
- tour: 4 to 8 steps that walk a newcomer through the map, most important part first.
- summary text is shown to the user on the board: keep it plain and short; no marketing words.
Write the file with the Write tool (it is the only file in .progress/ you edit by hand), then run `{H} map described`."""


def suggest_flows(g):
    """Starting points for flows: the screens that reach the most endpoints, with the chain the scan sees."""
    out = []
    for s in sorted(g.get("screens") or [], key=lambda s: -len(s["calls"]))[:4]:
        if not s["calls"]:
            continue
        calls = sorted(s["calls"], key=lambda k: ("POST", "PUT", "PATCH", "CALL", "AJAX", "GET").index(k.split()[0]) if k.split()[0] in ("POST", "PUT", "PATCH", "CALL", "AJAX", "GET") else 9)[:3]
        steps = [f"screen:{s['id']}"] + [f"ep:{k}" for k in calls] + \
                list(dict.fromkeys(f"part:{g['endpoints'][k]['part']}" for k in calls if g["endpoints"].get(k, {}).get("part")))
        out.append(f"- {s['name']} ({s['route']}): " + " → ".join(steps))
    return out


def worksheet(arg=None):
    sc = scan()
    g = build(sc, quiet=True)
    arch = rj(os.path.join(MAP, "architecture.json"), None)
    style = (arch or {}).get("style", "developer")
    hint = "developer style: name the main classes or functions and what they do" if style == "developer" else "plain words for the app's owner, no jargon"
    out = []
    files = g["files"]

    def digest(paths):
        for p in paths:
            f = files.get(p) or {}
            bits = [f"{p} ({f.get('lines', 0)} lines)"]
            if f.get("defs"):
                bits.append("defines " + ", ".join(f["defs"][:10]))
            if f.get("uses"):
                bits.append("uses " + ", ".join(f["uses"][:8]))
            if f.get("calls"):
                bits.append("calls " + ", ".join(f["calls"][:6]))
            if f.get("hosts"):
                bits.append("talks to " + ", ".join(f["hosts"][:5]))
            serves = [k for k, v in g["endpoints"].items() if v["file"] == p]
            if serves:
                bits.append("serves " + ", ".join(serves[:8]))
            if f.get("unused"):
                bits.append("never imported")
            out.append("- " + "; ".join(bits))

    def screens_and_flows():
        if g.get("screens"):
            out.append(f"\nScreens ({len(g['screens'])}): " + "; ".join(f"screen:{s['id']} = {s['name']} {s['route']} in {s['file']}" for s in g["screens"][:60]))
        sug = suggest_flows(g)
        have = {fl["id"] for fl in g.get("flows") or []}
        if sug and len(have) < 2:
            out.append("\nSuggested flows to write (check the code, then describe 2 to 4 of them in `flows`):")
            out.extend(sug)
        if not g.get("tour"):
            out.append("\nNo tour yet: add `tour` with 4 to 8 steps.")
        if g.get("dropped"):
            out.append("\nFlow and tour steps pointing at nodes that no longer exist (fix or remove): " + ", ".join(f"{a}: {b}" for a, b in g["dropped"][:20]))

    if g["auto"] or arg == "--all":
        out.append(f"# Map worksheet: {g['project']} (whole project)\n")
        out.append(RULES.format(style=style, style_hint=hint, H=H))
        out.append("\nSchema:\n" + SCHEMA)
        out.append("\nRead the README and entry points as needed, then write architecture.json. The scan found:\n")
        if g.get("repos"):
            out.append("Repos: " + ", ".join(g["repos"]))
        dirs = {}
        for p in files:
            if not files[p].get("config"):
                d = "/".join(p.split("/")[:2]) if p.count("/") >= 2 else p.split("/")[0] if "/" in p else "."
                dirs[d] = dirs.get(d, 0) + 1
        out.append("Folders (code files): " + ", ".join(f"{d} {n}" for d, n in sorted(dirs.items())))
        hosts = sorted({h for f in files.values() for h in f.get("hosts", [])})
        if hosts:
            out.append("Outside hosts in the code: " + ", ".join(hosts[:30]))
        pkgs = {}
        for f in files.values():
            for x in f.get("pkgs", []):
                n = "/".join(x.split("/")[:2]) if x.startswith("@") else x.split("/")[0]
                pkgs[n] = pkgs.get(n, 0) + 1
        if pkgs:
            out.append("Packages imported: " + ", ".join(f"{k} ({v})" for k, v in sorted(pkgs.items(), key=lambda x: -x[1])[:30]))
        out.append(f"\nEndpoints ({len(g['endpoints'])}): " + "; ".join(f"{k} in {v['file']}:{v['line']} ({v.get('auth', 'unknown')})" for k, v in list(g["endpoints"].items())[:80]))
        screens_and_flows()
        out.append("\nFiles:")
        digest([p for p in files if not files[p].get("config")])
    else:
        want = [p for p in g["parts"] if (arg and p["id"] == arg) or (not arg and p["id"] in g["stale"])]
        out.append(f"# Map worksheet: {g['project']}\n")
        out.append(RULES.format(style=style, style_hint=hint, H=H))
        out.append("\nEdit the existing architecture.json: keep what's still right, describe what's new or changed, drop entries for removed files.")
        if not want and not g["unassigned"]:
            out.append("\nNothing needs describing: the map is current. (`map worksheet --all` lists everything, `map worksheet PART` one part.)")
        for p in want:
            out.append(f"\n## {p['id']}: {p['name']} ({p['kind']})\nCurrent summary: {p['summary'] or '(none)'}")
            if p["new"]:
                out.append("New files:")
                digest(p["new"])
            if p["changed"]:
                out.append("Changed files (exports, endpoints or calls changed):")
                digest(p["changed"])
            if p["removed"]:
                out.append("Removed: " + ", ".join(p["removed"]))
            if arg:
                out.append("All files:")
                digest(p["files"])
            eps = [k for k in p["endpoints"] if not g["endpoints"][k].get("desc")]
            if eps:
                out.append("Endpoints without a description: " + ", ".join(eps))
        if g["unassigned"]:
            out.append("\n## Files in no part (add a path pattern to a part, or a new part):")
            digest(g["unassigned"][:200])
        screens_and_flows()
    print("\n".join(out))


def described():
    arch = rj(os.path.join(MAP, "architecture.json"), None)
    if not arch or not isinstance(arch.get("parts"), list) or not arch["parts"]:
        sys.exit("project-plan: write .progress/map/architecture.json first (see `map worksheet`).")
    problems, ids = [], set()
    for i, p in enumerate(arch["parts"]):
        if not isinstance(p, dict) or not str(p.get("id", "")).strip():
            problems.append(f"part {i + 1} has no id")
            continue
        if p["id"] in ids:
            problems.append(f"part id {p['id']} is used twice")
        ids.add(p["id"])
        if p.get("kind") not in KINDS:
            problems.append(f"part {p['id']}: kind must be one of {', '.join(KINDS)}")
        if not isinstance(p.get("paths", []), list):
            problems.append(f"part {p['id']}: paths must be a list of globs")
    for l in arch.get("links") or []:
        for end in ("from", "to"):
            if l.get(end) not in ids:
                problems.append(f"link {l.get('from')} → {l.get('to')}: no part called {l.get(end)!r}")
        if l.get("kind") and l["kind"] not in LINK_KINDS:
            problems.append(f"link {l.get('from')} → {l.get('to')}: kind must be one of {', '.join(LINK_KINDS)}")
    for fl in arch.get("flows") or []:
        if not isinstance(fl, dict) or not fl.get("id") or not isinstance(fl.get("steps"), list):
            problems.append(f"flow {fl.get('id') if isinstance(fl, dict) else fl!r}: needs an id and a list of steps")
    if not isinstance(arch.get("tour", []), list):
        problems.append("tour must be a list of {node, text} steps")
    if problems:
        sys.exit("project-plan: architecture.json needs fixing:\n- " + "\n- ".join(problems))
    sc = scan()
    arch["described"] = now()
    wj(os.path.join(MAP, "architecture.json"), arch)
    g = build(sc, quiet=True)
    owner = {p: f["part"] for p, f in g["files"].items()}
    wj(os.path.join(MAP, "described.json"), {p: {"hash": f.get("hash"), "sig": f.get("sig"), "part": owner.get(p)}
                                             for p, f in sc["files"].items() if not f.get("config")})
    g = build(sc, quiet=True)
    tools = {p["id"] for p in g["parts"] if p["kind"] == "tool"}
    missing = [p for p, f in g["files"].items() if ext(p) in CODE and f["part"] and f["part"] not in tools and not f["desc"]]
    print(summary(g))
    for p in [x for x in arch["parts"] if x.get("paths")]:
        if not g["parts"] or not next((q for q in g["parts"] if q["id"] == p["id"] and q["files"]), None):
            print(f"Part {p['id']} matched no files: check its paths.")
    if missing:
        print(f"{len(missing)} files have no description yet: {', '.join(missing[:10])}{' …' if len(missing) > 10 else ''}")
    if g.get("dropped"):
        print(f"Dropped {len(g['dropped'])} flow/tour step{'s' if len(g['dropped']) != 1 else ''} whose node doesn't exist: "
              + ", ".join(f"{a} → {b}" for a, b in g["dropped"][:10]) + ". Use part:, screen:, ep: or file: ids from the map.")
    if not g.get("flows") and g.get("screens"):
        print("No flows yet: add 2 to 4 under `flows` (`map worksheet` suggests some).")
    print(f"The board's Map view shows it: open the board and press Map.")


def load():
    g = rj(os.path.join(MAP, "graph.json"), None)
    if not g:
        g = build(quiet=True)
    return g


def overview(g=None, limit=None):
    g = g or load()
    parts = {p["id"]: p for p in g["parts"]}
    out = [f"Project map of {g['project']}{' (folders only: not described yet)' if g['auto'] else ''}."]
    if g.get("summary"):
        out.append(g["summary"])
    out.append("\nParts:")
    for p in g["parts"]:
        out.append(f"- {p['id']} · {p['name']} [{p['kind']}] {len(p['files'])} files, {len(p['endpoints'])} endpoints: {p['summary']}".rstrip(": "))
    out.append("\nConnections:")
    for e in g["edges"]:
        why = ", ".join(x for x in (e["label"], f"{e['imports']} imports" if e["imports"] else "", f"{e['calls']} requests" if e["calls"] else "") if x)
        out.append(f"- {parts[e['from']]['name']} → {parts[e['to']]['name']} ({why})")
    if g.get("screens"):
        out.append("\nScreens: " + ", ".join(f"{s['name']} ({s['route']})" for s in g["screens"][:30]))
    if g.get("flows"):
        out.append("\nFlows: " + ", ".join(fl["name"] for fl in g["flows"]))
    h = g["health"]
    out.append(f"\nHealth: {h['high']} high, {h['medium']} medium, {h['low']} low" + "".join(f"\n- [{i['severity']}] {i['title']}" for i in h["issues"] if not i["dismissed"]))
    return "\n".join(out)


def find(words):
    g = load()
    q = [w.lower() for w in words if w]
    hits = []
    for p in g["parts"]:
        hay = f"{p['id']} {p['name']} {p['summary']}".lower()
        if all(w in hay for w in q):
            hits.append((0, f"part {p['id']}: {p['name']} · {p['summary']}"))
    for s in g.get("screens") or []:
        hay = f"{s['id']} {s['name']} {s['route']} {s['file']}".lower()
        if all(w in hay for w in q):
            hits.append((1, f"screen {s['id']}: {s['name']} {s['route']} → {s['file']}"))
    for path, f in g["files"].items():
        hay = f"{path} {f['desc']} {' '.join(f['defs'])}".lower()
        if all(w in hay for w in q):
            hits.append((2, f"file {path} [{f['part'] or 'no part'}]{': ' + f['desc'] if f['desc'] else ''}"))
    for k, v in g["endpoints"].items():
        hay = f"{k} {v.get('desc', '')} {v.get('handler', '')}".lower()
        if all(w in hay for w in q):
            hits.append((3, f"endpoint {k} → {v['file']}:{v['line']} ({v.get('auth', 'unknown')}){': ' + v['desc'] if v.get('desc') else ''}"))
    hits.sort()
    print("\n".join(h for _, h in hits[:25]) or "Nothing in the map matches. Search the code instead.")


def show(thing):
    g = load()
    parts = {p["id"]: p for p in g["parts"]}
    thing = thing.split(":", 1)[1] if re.match(r"^(part|file|ep|screen):", thing) else thing
    if thing in parts:
        p = parts[thing]
        out = [f"{p['name']} [{p['kind']}] · {p['summary']}"]
        ins = [e for e in g["edges"] if e["to"] == p["id"]]
        outs = [e for e in g["edges"] if e["from"] == p["id"]]
        if outs:
            out.append("Uses: " + ", ".join(parts[e["to"]]["name"] + (f" ({e['label']})" if e["label"] else "") for e in outs))
        if ins:
            out.append("Used by: " + ", ".join(parts[e["from"]]["name"] + (f" ({e['label']})" if e["label"] else "") for e in ins))
        out.append("Files:")
        out += [f"- {x}{': ' + g['files'][x]['desc'] if g['files'][x]['desc'] else ''}" for x in p["files"]]
        if p["endpoints"]:
            out.append("Endpoints:")
            out += [f"- {k} ({g['endpoints'][k]['file']}:{g['endpoints'][k]['line']}){': ' + g['endpoints'][k]['desc'] if g['endpoints'][k].get('desc') else ''}" for k in p["endpoints"]]
        return print("\n".join(out))
    s = next((s for s in g.get("screens") or [] if s["id"] == thing or s["route"] == thing), None)
    if s:
        out = [f"{s['name']} · {s['route']} [{s['framework']}] · {s['file']}"]
        out.append("Files: " + ", ".join(s["files"]))
        if s["calls"]:
            out.append("Calls: " + ", ".join(s["calls"]))
        return print("\n".join(out))
    f = g["files"].get(thing.lstrip("./"))
    if f:
        out = [f"{thing} · part {f['part'] or '(none)'} · {f['lines']} lines{' · never imported' if f.get('unused') else ''}", f["desc"] or "(no description)"]
        for label, key in (("Defines", "defs"), ("Uses", "uses"), ("Used by", "usedBy"), ("Calls", "calls"), ("Packages", "pkgs"), ("Talks to", "hosts")):
            if f.get(key):
                out.append(f"{label}: {', '.join(f[key])}")
        serves = [k for k, v in g["endpoints"].items() if v["file"] == thing]
        if serves:
            out.append("Serves: " + ", ".join(serves))
        on = [s["name"] for s in g.get("screens") or [] if thing in s["files"]]
        if on:
            out.append("On screens: " + ", ".join(on))
        return print("\n".join(out))
    eps = [(k, v) for k, v in g["endpoints"].items() if thing.lower() in k.lower()]
    if eps:
        for k, v in eps[:10]:
            print(f"{k} → {v['file']}:{v['line']}{' ' + v['handler'] if v.get('handler') else ''} [{v.get('part') or 'no part'}] · {v.get('auth', 'unknown')}"
                  f"{chr(10) + '  ' + v['desc'] if v.get('desc') else ''}{chr(10) + '  called from ' + ', '.join(v['callers']) if v.get('callers') else ''}")
        return
    print(f"Nothing called {thing!r} in the map. Try `{H} map find WORDS`.")


def resolve_node(g, thing):
    """A user's words for a node ('rest-api', 'src/a.js', 'POST /x', 'checkout') as a set of node ids."""
    thing = thing.strip()
    if re.match(r"^(part|file|ep|screen):", thing):
        return {thing} if node_ok(thing, g) else set()
    if any(p["id"] == thing for p in g["parts"]):
        return {"part:" + thing}
    if thing.lstrip("./") in g["files"]:
        return {"file:" + thing.lstrip("./")}
    if thing in g["endpoints"]:
        return {"ep:" + thing}
    s = [x for x in g.get("screens") or [] if thing in (x["id"], x["route"])]
    if s:
        return {"screen:" + s[0]["id"]}
    eps = [k for k in g["endpoints"] if thing.lower() in k.lower()]
    if len(eps) == 1:
        return {"ep:" + eps[0]}
    fs = [p for p in g["files"] if p.endswith("/" + thing) or os.path.basename(p) == thing]
    return {"file:" + fs[0]} if len(fs) == 1 else set()


def path_between(a, b, g=None):
    """The shortest chain of uses/calls from node a to node b, as [(node, how)], or None."""
    g = g or load()
    nxt = {}

    def link(x, y, how):
        nxt.setdefault(x, []).append((y, how))
    for p, f in g["files"].items():
        for u in f.get("uses", []):
            link("file:" + p, "file:" + u, "uses")
        for c in f.get("calls", []):
            link("file:" + p, "ep:" + c, "calls")
    for k, v in g["endpoints"].items():
        link("ep:" + k, "file:" + v["file"], "handled in")
    for s in g.get("screens") or []:
        link("screen:" + s["id"], "file:" + s["file"], "rendered by")
        for q in s["files"][1:]:
            link("screen:" + s["id"], "file:" + q, "shows")
    for pt in g["parts"]:
        for q in pt["files"]:
            link("part:" + pt["id"], "file:" + q, "contains")
        for k in pt["endpoints"]:
            link("part:" + pt["id"], "ep:" + k, "serves")
    for e in g["edges"]:
        if e.get("label") or e.get("kind"):
            link("part:" + e["from"], "part:" + e["to"], e.get("label") or e.get("kind") or "uses")
    targets = set(b)
    for t in list(b):
        if t.startswith("part:"):
            pid = t[5:]
            targets |= {"file:" + q for q, f in g["files"].items() if f.get("part") == pid} | {"ep:" + k for k, v in g["endpoints"].items() if v.get("part") == pid}
    prev = {x: None for x in a}
    todo = deque(a)
    while todo:
        x = todo.popleft()
        if x in targets and x not in a:
            chain = []
            while x is not None:
                chain.append(x)
                x = prev[x] and prev[x][0]
            chain.reverse()
            return [(n, prev[n][1] if prev[n] else "") for n in chain]
        for y, how in nxt.get(x, []):
            if y not in prev:
                prev[y] = (x, how)
                todo.append(y)
    return None


def path_cmd(a, b):
    g = load()
    na, nb = resolve_node(g, a), resolve_node(g, b)
    for want, got in ((a, na), (b, nb)):
        if not got:
            return print(f"Nothing in the map is called {want!r}. Use a part id, a file path, an endpoint key or a screen id (`{H} map find WORDS`).")
    chain, back = path_between(na, nb, g), False
    if not chain:
        chain, back = path_between(nb, na, g), True
    if not chain:
        return print(f"No chain of imports or requests connects {a} and {b}.")
    if back:
        print(f"No path from {a} to {b}; the other way round:")
    for i, (n, how) in enumerate(chain):
        print(("  " if i else "") + (f"{how} → " if how else "") + n)


# ---------------------------------------------------------------- cli

def cli(args):
    cmd, rest = (args[0] if args else "overview"), args[1:]
    if cmd == "update":
        if "--if-stale" in rest:  # the automatic rescan: at most once a minute, and only for projects that have a map
            g = os.path.join(MAP, "graph.json")
            if not os.path.exists(g) or time.time() - os.path.getmtime(g) < 60:
                return
        return build(quiet="--quiet" in rest or "--if-stale" in rest)
    if cmd == "worksheet":
        return worksheet(rest[0] if rest else None)
    if cmd == "described":
        return described()
    if cmd == "overview":
        return print(overview())
    if cmd == "find":
        return find(rest)
    if cmd == "show" and rest:
        return show(" ".join(rest))
    if cmd == "path" and len(rest) >= 2:
        if len(rest) > 2 and re.fullmatch(r"[A-Z]+", rest[0]):  # an endpoint key given as two words: POST /x
            rest = [rest[0] + " " + rest[1]] + rest[2:]
        if len(rest) > 2 and re.fullmatch(r"[A-Z]+", rest[1]):
            rest = [rest[0], rest[1] + " " + rest[2]]
        return path_cmd(rest[0], rest[1])
    if cmd == "ask" and rest:
        import mapapp
        r = mapapp.ask(" ".join(rest))
        return print(r.get("error") or f"{r['answer']}\n\nParts: {', '.join(r.get('parts') or []) or '-'} · ${r.get('cost') or 0:.3f}")
    sys.exit(__doc__)
