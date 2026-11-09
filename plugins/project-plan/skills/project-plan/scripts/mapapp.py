"""The map's AI and sharing side: Ask (answer boards), flows on request, blueprints, export and notes.

codemap.py builds the map from the code; this module adds what needs Claude (or Codex), and what the
board's server serves. CLI (through hook.py map …):

    map ask "QUESTION"        answer a question about the code (read-only)
    map flow "WHAT"           trace a flow through the code and save it to the map
    map blueprint PRD         print how to plan an app from a PRD into .progress/map/plan.json
    map planned               check plan.json and draw it as a blueprint map
    map export [FILE]         write the map as one HTML file to send to anyone
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

import codemap
from codemap import now, rj, wj, KINDS

SKILL = os.path.dirname(os.path.realpath(__file__))
H = "python3 ~/.claude/project-plan/hook.py"


def mp(name):
    return os.path.join(codemap.MAP, name)


def load():
    return codemap.load()


def overview(g=None):
    return codemap.overview(g)


def blueprint_mode():
    return os.path.exists(mp("plan.json")) and rj(mp("graph.json"), {}).get("blueprint", True)


# ---------------------------------------------------------------- running Claude (or Codex) read-only

def which(name, extra=()):
    for c in (shutil.which(name), *extra):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def claude_bin():
    return which("claude", (os.path.expanduser("~/.claude/local/claude"), os.path.expanduser("~/.local/bin/claude"),
                            "/opt/homebrew/bin/claude", "/usr/local/bin/claude"))


def run_ai(prompt, timeout=300):
    """Run a read-only helper in the project: Claude Code if installed, else Codex. Returns (text, cost, who)."""
    env = dict(os.environ, PROJECT_PLAN_OFF="1")  # the helper run isn't a session for the board
    exe = claude_bin()
    if exe:
        r = subprocess.run([exe, "-p", prompt, "--output-format", "json", "--allowedTools", "Read,Grep,Glob",
                            "--disallowedTools", "Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch,Agent,Task"],
                           cwd=codemap.ROOT, env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        try:
            res = json.loads(r.stdout)
        except ValueError:
            raise RuntimeError((r.stderr or r.stdout or "claude -p failed").strip()[-400:])
        if res.get("is_error") or not res.get("result"):
            raise RuntimeError(res.get("result") or "Claude returned no answer.")
        return res["result"], res.get("total_cost_usd") or 0, "Claude Code"
    exe = which("codex", ("/opt/homebrew/bin/codex", "/usr/local/bin/codex"))
    if exe:
        with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False) as out:
            path = out.name
        try:
            r = subprocess.run([exe, "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--output-last-message", path, prompt],
                               cwd=codemap.ROOT, env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
            text = open(path).read().strip()
        finally:
            os.unlink(path)
        if not text:
            raise RuntimeError((r.stderr or "codex exec failed").strip()[-400:])
        return text, 0, "Codex"
    raise RuntimeError("Neither the claude nor the codex command is installed on this computer.")


def json_block(text):
    """The last JSON object in a reply (in a ```json block or bare)."""
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    for cand in reversed(blocks or [text[text.find("{"):text.rfind("}") + 1]]):
        try:
            return json.loads(cand)
        except ValueError:
            continue
    raise RuntimeError("The answer wasn't in the expected format; try asking again.")


def node_ids(g):
    ids = {f"part:{p['id']}" for p in g["parts"]} | {f"file:{p}" for p in g["files"]} | {f"ep:{k}" for k in g["endpoints"]}
    ids |= {f"screen:{s['id']}" for s in g.get("screens") or []}
    return ids


def describe_node(g, nid):
    kind, _, key = nid.partition(":")
    if kind == "part":
        p = next((x for x in g["parts"] if x["id"] == key), None)
        return p and f"{nid}: part {p['name']} ({p['kind']}): {p['summary']}; files {', '.join(p['files'][:15])}"
    if kind == "file" and key in g["files"]:
        f = g["files"][key]
        return f"{nid}: {f.get('desc') or ''} defines {', '.join(f.get('defs', [])[:10])}; uses {', '.join(f.get('uses', [])[:8])}"
    if kind == "ep" and key in g["endpoints"]:
        e = g["endpoints"][key]
        return f"{nid}: handled in {e['file']}:{e['line']}; {e.get('desc', '')}; auth {e.get('auth', 'unknown')}"
    if kind == "screen":
        s = next((x for x in g.get("screens") or [] if x["id"] == key), None)
        return s and f"{nid}: screen {s['name']} {s.get('route', '')} in {s['file']}"
    if kind == "group":
        return f"{nid}: the folder {key.split(':', 1)[-1]}"
    return None


NODE_HELP = ("Node ids: `part:<id>`, `file:<path>`, `ep:<METHOD> <path>` (endpoint key exactly as listed), `screen:<id>`. "
             "Use only ids that exist in the map.")


def ask(question, history=None, context=None, mode="ask"):
    """Answer a question about the code. Returns {answer, nodes, followups, parts, cost, at} or {error}."""
    g = load()
    if mode == "change":
        return change(question)
    ctx = [d for d in (describe_node(g, n) for n in context or []) if d]
    prior = "".join(f"\nEarlier question: {h.get('q', '')}\nYour answer: {(h.get('answer') or h.get('a') or '')[:1200]}\n" for h in (history or [])[-3:])
    where = ("This is a blueprint: nothing is built yet. Answer from the plan below and the PRD it names "
             f"({g.get('plan', {}).get('source', 'unknown')}).") if g.get("blueprint") else "Read the code to check before you answer; don't guess."
    prompt = (f"You answer questions about this codebase for its developer. {where}\n\n{overview(g)}\n"
              + (f"\nThe question is about:\n" + "\n".join(ctx) + "\n" if ctx else "") + prior +
              f"\nQuestion: {question}\n\nReply with only a JSON object in a ```json block:\n"
              '{"answer": "markdown, under 250 words, cite code as path:line", '
              '"nodes": [{"id": "node id", "note": "what this piece does in the answer, under 15 words", "step": 1}], '
              '"followups": ["2 or 3 short follow-up questions"]}\n'
              f"Give `step` numbers (1, 2, …) when the answer is a sequence, else null. List 2 to 12 nodes. {NODE_HELP}")
    try:
        text, cost, who = run_ai(prompt)
        res = json_block(text)
    except subprocess.TimeoutExpired:
        return {"error": "Took longer than 5 minutes; try a narrower question."}
    except RuntimeError as e:
        return {"error": str(e)}
    known = node_ids(g)
    nodes = [{"id": n["id"], "note": str(n.get("note", ""))[:200], "step": n.get("step") if isinstance(n.get("step"), int) else None}
             for n in res.get("nodes") or [] if isinstance(n, dict) and (n.get("id") in known or str(n.get("id", "")).startswith("group:"))]
    out = {"q": question, "answer": str(res.get("answer", "")).strip(), "nodes": nodes,
           "followups": [str(x) for x in res.get("followups") or []][:4], "cost": cost, "by": who, "at": now()}
    out["parts"] = sorted({codemap_part(g, n["id"]) for n in nodes} - {None})
    wj(mp("asks.json"), (asks() + [out])[-20:])
    return out


def asks():
    """Saved answers, upgrading the first version's {q, a, parts} entries."""
    out = []
    for a in rj(mp("asks.json"), []):
        if "a" in a and "answer" not in a:
            a = {"q": a.get("q", ""), "answer": a["a"], "nodes": [{"id": f"part:{p}", "note": "", "step": None} for p in a.get("parts", [])],
                 "followups": [], "cost": a.get("cost", 0), "at": a.get("at", "")}
        out.append(a)
    return out


def codemap_part(g, nid):
    kind, _, key = nid.partition(":")
    if kind == "part":
        return key
    if kind == "file":
        return (g["files"].get(key) or {}).get("part")
    if kind == "ep":
        return (g["endpoints"].get(key) or {}).get("part")
    return None


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "flow"


def flow(question):
    """Trace a flow ("how does a refund reach the card?") and save it with the map. Returns {flow} or {error}."""
    g = load()
    prompt = (f"Trace this flow through the codebase for its developer: {question}\n"
              f"Read the code to follow it step by step.\n\n{overview(g)}\n\n"
              "Reply with only a JSON object in a ```json block:\n"
              '{"name": "short name", "summary": "one sentence", "steps": [{"node": "node id", "text": "what happens here, one or two sentences, cite path:line"}]}\n'
              f"Use 3 to 10 steps in order. {NODE_HELP}")
    try:
        text, cost, _ = run_ai(prompt)
        res = json_block(text)
    except subprocess.TimeoutExpired:
        return {"error": "Took longer than 5 minutes; try a narrower flow."}
    except RuntimeError as e:
        return {"error": str(e)}
    known = node_ids(g)
    steps = [{"node": s["node"], "text": str(s.get("text", ""))[:600]} for s in res.get("steps") or []
             if isinstance(s, dict) and s.get("node") in known]
    if not steps:
        return {"error": "Couldn't trace that flow through the map; try naming a screen or an endpoint."}
    flows = rj(mp("flows.json"), [])
    fid, n = slug(res.get("name") or question), 2
    while any(f["id"] == fid for f in flows + (g.get("flows") or [])):
        fid, n = f"{slug(res.get('name') or question)}-{n}", n + 1
    f = {"id": fid, "name": str(res.get("name") or question)[:80], "summary": str(res.get("summary", ""))[:300],
         "steps": steps, "requested": True, "q": question, "cost": cost, "at": now()}
    wj(mp("flows.json"), flows + [f])
    g.setdefault("flows", []).append(f)  # show it now, before the next rebuild merges flows.json
    wj(mp("graph.json"), g)
    return {"flow": f, "cost": cost}


def note(path, text):
    notes = rj(mp("notes.json"), {})
    if text.strip():
        notes[path] = {"text": text.strip()[:4000], "at": now()}
    else:
        notes.pop(path, None)
    wj(mp("notes.json"), notes)


# ---------------------------------------------------------------- export

def export(flows=True, notes=True, recent=False, health=False, history_text=""):
    """The map as one HTML file: no Ask, no requests, no folder path, nothing about this computer."""
    g = json.loads(json.dumps(load()))
    g.pop("root", None)
    if not health:
        g["health"] = {"issues": [], "high": 0, "medium": 0, "low": 0}
        g["review"] = []
    else:
        for i in g["health"].get("issues", []):
            i.pop("reason", None)  # the user's reasons stay private
    if not flows:
        g["flows"], g["tour"] = [], []
    data = {"graph": g, "notes": rj(mp("notes.json"), {}) if notes else {}, "exported": now()}
    if recent:
        data["history"] = history_text
    html = open(os.path.join(SKILL, "map.html")).read()
    blob = json.dumps(data).replace("</", "<\\/")
    i = html.find("<head>")
    i = i + len("<head>") if i >= 0 else 0
    return html[:i] + f"\n<script>window.MAPDATA = {blob};</script>" + html[i:]


# ---------------------------------------------------------------- blueprints

PLAN_SCHEMA = """{
  "source": "docs/prd.md", "summary": "Two or three sentences: what will be built.",
  "stack": ["Next.js", "Postgres", "Stripe"], "assumptions": ["…"], "questions": ["Open questions the PRD leaves"],
  "parts": [{"id": "checkout", "name": "Checkout", "kind": "screen|server|data|service|tool|other", "summary": "…",
             "files": [{"path": "app/checkout/page.tsx", "desc": "…"}]}],
  "endpoints": [{"method": "POST", "path": "/api/orders", "part": "orders", "file": "app/api/orders/route.ts",
                 "desc": "…", "auth": "public|signed-in|admin|service|undecided", "changes_data": true}],
  "screens": [{"id": "checkout", "name": "Checkout", "route": "/checkout", "part": "checkout", "calls": ["POST /api/orders"]}],
  "links": [{"from": "orders", "to": "stripe", "label": "PaymentIntent", "kind": "uses|request|job|webhook|queue|event|data"}],
  "flows": [{"id": "buy", "name": "Buying", "summary": "…", "steps": [{"node": "screen:checkout", "text": "…"}]}],
  "tour": [{"node": "part:checkout", "text": "…"}]
}"""


def blueprint_worksheet(prd):
    print(f"""# Blueprint from {prd}

Read {prd} fully, then plan the app it describes and write .progress/map/plan.json (the Write tool):

{PLAN_SCHEMA}

- 4 to 12 parts, like a developer would explain the system; outside services (payments, email, auth providers) are parts with kind "service" and no files.
- Every file the system would need, with a one-line description; every endpoint with who may call it (`undecided` when the PRD doesn't say).
- Which screen calls which endpoint; links for what the code wouldn't show (webhooks, jobs, queues).
- Pick a stack and list it; list your assumptions and the questions the PRD leaves open.
- 2 to 4 flows of the main jobs, and a short tour (node ids: part:<id>, file:<path>, ep:<METHOD> <path>, screen:<id>).
Then run `{H} map planned` and fix anything it reports. Once the code exists, `{H} map update --from-code` maps the real code (plan.json stays).""")


def check_plan(plan):
    problems = []
    if not isinstance(plan, dict) or not isinstance(plan.get("parts"), list) or not plan["parts"]:
        return ["plan.json needs a non-empty \"parts\" list"]
    ids = set()
    for i, p in enumerate(plan["parts"], 1):
        if not isinstance(p, dict) or not str(p.get("id", "")).strip():
            problems.append(f"part {i} has no id")
            continue
        if p["id"] in ids:
            problems.append(f"part id {p['id']} is used twice")
        ids.add(p["id"])
        if p.get("kind") not in KINDS:
            problems.append(f"part {p['id']}: kind must be one of {', '.join(KINDS)}")
    for e in plan.get("endpoints") or []:
        if not e.get("method") or not str(e.get("path", "")).startswith("/"):
            problems.append(f"endpoint {e}: needs method and a path starting with /")
        if e.get("part") not in ids:
            problems.append(f"endpoint {e.get('method')} {e.get('path')}: no part called {e.get('part')!r}")
    keys = {f"{e.get('method', '').upper()} {e.get('path')}" for e in plan.get("endpoints") or []}
    for s in plan.get("screens") or []:
        for c in s.get("calls") or []:
            if c not in keys:
                problems.append(f"screen {s.get('id')}: calls {c!r}, which isn't in endpoints")
    for l in plan.get("links") or []:
        for end in ("from", "to"):
            if l.get(end) not in ids:
                problems.append(f"link {l.get('from')} → {l.get('to')}: no part called {l.get(end)!r}")
    return problems


def plan_graph(plan):
    """Draw plan.json as a map: the same graph shape as code, with blueprint: true and a design review."""
    parts, files, eps, edges = [], {}, {}, {}
    for p in plan["parts"]:
        fl = [f if isinstance(f, dict) else {"path": str(f)} for f in p.get("files") or []]
        for f in fl:
            files[f["path"]] = {"part": p["id"], "repo": "", "desc": f.get("desc", ""), "lines": 0, "defs": [], "uses": [], "usedBy": [],
                                "calls": [], "pkgs": [], "hosts": [], "unused": False, "config": False}
        parts.append({"id": p["id"], "name": p.get("name") or p["id"], "kind": p.get("kind", "other"), "summary": p.get("summary", ""),
                      "external": p.get("kind") == "service" and not fl, "files": [f["path"] for f in fl], "endpoints": [], "lines": 0,
                      "new": [], "changed": [], "removed": []})
    byid = {p["id"]: p for p in parts}
    for e in plan.get("endpoints") or []:
        key = f"{e['method'].upper()} {e['path']}"
        auth = e.get("auth") or "undecided"
        eps[key] = {"method": e["method"].upper(), "path": e["path"], "kind": "http", "file": e.get("file", ""), "line": 1, "handler": "",
                    "part": e["part"], "desc": e.get("desc", ""), "callers": [], "auth": auth, "public": auth == "public",
                    "changes_data": bool(e.get("changes_data", e["method"].upper() not in ("GET", "HEAD")))}
        byid[e["part"]]["endpoints"].append(key)
        if e.get("file") and e["file"] not in files:
            files[e["file"]] = {"part": e["part"], "repo": "", "desc": "", "lines": 0, "defs": [], "uses": [], "usedBy": [], "calls": [],
                                "pkgs": [], "hosts": [], "unused": False, "config": False}
            byid[e["part"]]["files"].append(e["file"])

    def edge(a, b, kind, label="", calls=0):
        if a in byid and b in byid and a != b:
            x = edges.setdefault((a, b), {"from": a, "to": b, "imports": 0, "calls": 0, "label": "", "kind": kind})
            x["calls"] += calls
            if label:
                x["label"], x["kind"] = label, kind
    screens = []
    for s in plan.get("screens") or []:
        calls = [c for c in s.get("calls") or [] if c in eps]
        for c in calls:
            eps[c]["callers"].append(s.get("part") or "")
            edge(s.get("part"), eps[c]["part"], "request", calls=1)
        screens.append({"id": s["id"], "name": s.get("name") or s["id"], "route": s.get("route", ""), "file": "",
                        "framework": "planned", "files": byid.get(s.get("part"), {}).get("files", [])[:20], "calls": calls})
    for l in plan.get("links") or []:
        edge(l["from"], l["to"], l.get("kind") or "uses", l.get("label") or "uses")

    review = []

    def issue(rule, sev, title, detail, fix, locs):
        review.append({"id": f"{rule}:{slug(title)}", "rule": rule, "severity": sev, "title": title, "detail": detail, "fix": fix,
                       "locations": locs, "parts": sorted({codemap_part({"files": files, "endpoints": eps}, l["node"]) for l in locs} - {None}),
                       "dismissed": False, "reason": ""})
    for k, e in eps.items():
        if e["auth"] == "public" and e["changes_data"]:
            issue("open-write", "high", f"Anyone could change data through {k}", "It's planned as public and it changes data.",
                  f"Decide who may call {k}, or how it's protected if it must stay public (signature, rate limit, captcha).",
                  [{"file": e["file"], "line": 0, "text": k, "node": f"ep:{k}"}])
        elif e["auth"] == "undecided":
            issue("who-may", "medium", f"No decision on who may call {k}", "The plan doesn't say who may use this endpoint.",
                  f"Decide who may call {k}: anyone, signed-in users, admins, or another service.",
                  [{"file": e["file"], "line": 0, "text": k, "node": f"ep:{k}"}])
    for q in plan.get("questions") or []:
        issue("question", "medium", str(q)[:140], "An open question the PRD leaves.", f"Answer this question and update the plan: {q}", [])
    linked = {x for e in edges.values() for x in (e["from"], e["to"])}
    for p in parts:
        if p["id"] not in linked and len(parts) > 1:
            issue("orphan", "low", f"Nothing connects to {p['name']}", "No screen, endpoint or link uses this part.",
                  f"Say how {p['name']} fits in: what uses it and what it uses.", [{"file": "", "line": 0, "text": p["name"], "node": f"part:{p['id']}"}])
    g = {"built": now(), "scanned": now(), "described": now(), "auto": False, "blueprint": True,
         "project": os.path.basename(codemap.ROOT), "root": codemap.ROOT, "repos": [], "style": "developer",
         "summary": plan.get("summary", ""), "parts": parts, "edges": list(edges.values()), "files": files, "endpoints": eps,
         "screens": screens, "flows": (plan.get("flows") or []) + rj(mp("flows.json"), []), "tour": plan.get("tour") or [],
         "unassigned": [], "stale": [], "health": {"issues": [], "high": 0, "medium": 0, "low": 0}, "review": review,
         "plan": {k: plan.get(k) for k in ("source", "stack", "assumptions", "questions")}}
    return g


def planned(quiet=False):
    plan = rj(mp("plan.json"), None)
    problems = check_plan(plan)
    if problems:
        raise ValueError("plan.json needs fixing:\n- " + "\n- ".join(problems))
    g = plan_graph(plan)
    wj(mp("graph.json"), g)
    if not quiet:
        r = g["review"]
        print(f"Blueprint of {g['project']}: {len(g['parts'])} parts, {len(g['files'])} files, {len(g['endpoints'])} endpoints, "
              f"{len(g['screens'])} screens. Design review: {sum(i['severity'] == 'high' for i in r)} high, "
              f"{sum(i['severity'] == 'medium' for i in r)} medium, {sum(i['severity'] == 'low' for i in r)} low. Open the board's Map tab.")
    return g


def change(request):
    """Reshape a blueprint from the board ("also plan out refunds"). Returns {summary, changed, followups, cost} or {error}."""
    plan = rj(mp("plan.json"), None)
    if not plan:
        return {"error": "Change works on a blueprint; this map is drawn from code."}
    base = (f"Here is the plan of an app that isn't built yet (from {plan.get('source', 'a PRD')}, which you may read):\n"
            f"```json\n{json.dumps(plan, indent=1)}\n```\n\nChange the plan as asked: {request}\n\n"
            "Reply with only a JSON object in a ```json block: "
            '{"plan": <the whole updated plan, same format>, "summary": "what you changed, two or three sentences", '
            '"followups": ["2 or 3 suggested next changes"]}')
    cost, prompt = 0, base
    for attempt in range(2):  # one retry with the problems sent back
        try:
            text, c, _ = run_ai(prompt, timeout=600)
            cost += c
            res = json_block(text)
        except subprocess.TimeoutExpired:
            return {"error": "Took longer than 10 minutes; try a smaller change."}
        except RuntimeError as e:
            return {"error": str(e)}
        problems = check_plan(res.get("plan"))
        if not problems:
            break
        prompt = base + "\n\nYour last plan had these problems; fix them:\n- " + "\n- ".join(problems)
    else:
        return {"error": "The changed plan didn't check out: " + "; ".join(problems[:5])}
    old_g = plan_graph(plan)
    shutil.copyfile(mp("plan.json"), mp("plan.prev.json"))
    wj(mp("plan.json"), res["plan"])
    g = planned(quiet=True)
    changed = []
    for p in g["parts"]:
        o = next((x for x in old_g["parts"] if x["id"] == p["id"]), None)
        if not o or (o["summary"], o["files"], o["endpoints"]) != (p["summary"], p["files"], p["endpoints"]):
            changed.append(f"part:{p['id']}")
    changed += [f"ep:{k}" for k, e in g["endpoints"].items() if old_g["endpoints"].get(k, {}).get("desc") != e["desc"] or k not in old_g["endpoints"]]
    changed += [f"file:{k}" for k in g["files"] if k not in old_g["files"]]
    return {"summary": str(res.get("summary", "")), "changed": changed, "followups": [str(x) for x in res.get("followups") or []][:4],
            "cost": cost, "at": now()}


def undo():
    if not os.path.exists(mp("plan.prev.json")):
        raise ValueError("Nothing to undo.")
    os.replace(mp("plan.prev.json"), mp("plan.json"))
    planned(quiet=True)


# ---------------------------------------------------------------- blueprints in proposals

def blueprint(spec):
    """Validate a proposal's planned map: {"parts": [{id, name, kind, summary, status}], "links": [{from, to, label}]}."""
    if not spec:
        return None
    if not isinstance(spec, dict) or not isinstance(spec.get("parts"), list):
        raise ValueError('"map" must be {"parts": [...], "links": [...]}')
    parts = []
    for i, p in enumerate(spec["parts"][:30], 1):
        if not isinstance(p, dict) or not str(p.get("id", "")).strip():
            raise ValueError(f"map part {i} needs an id")
        st = p.get("status") or "new"
        if st not in ("new", "changed", "existing", "removed"):
            raise ValueError(f'map part {p["id"]}: status must be new, changed, existing or removed')
        parts.append({"id": str(p["id"]), "name": str(p.get("name") or p["id"]), "kind": p.get("kind") if p.get("kind") in KINDS else "other",
                      "summary": str(p.get("summary", ""))[:300], "status": st, "files": [str(x) for x in (p.get("files") or [])][:30]})
    ids = {p["id"] for p in parts}
    links = [{"from": str(l["from"]), "to": str(l["to"]), "label": str(l.get("label", ""))[:120]}
             for l in spec.get("links") or [] if isinstance(l, dict) and l.get("from") in ids and l.get("to") in ids]
    return {"parts": parts, "links": links}


# ---------------------------------------------------------------- cli

def cli(args):
    """`hook.py map …` commands that live here; returns False when the command isn't one of ours."""
    cmd, rest = (args[0] if args else ""), args[1:]
    if cmd == "update" and blueprint_mode() and "--from-code" not in rest:
        planned(quiet="--quiet" in rest or "--if-stale" in rest)
        return True
    if cmd == "update" and "--from-code" in rest:
        g = rj(mp("graph.json"), {})
        if g.get("blueprint"):
            os.remove(mp("graph.json"))  # from now on the map follows the code; plan.json stays
        codemap.build(quiet=False)
        return True
    if cmd == "ask" and rest:
        r = ask(" ".join(rest))
        print(r.get("error") or f"{r['answer']}\n\nNodes: {', '.join(n['id'] for n in r['nodes']) or '-'} · ${r['cost']:.3f}")
        return True
    if cmd == "flow" and rest:
        r = flow(" ".join(rest))
        print(r.get("error") or f"Flow {r['flow']['name']}: {len(r['flow']['steps'])} steps, saved. It's under Flows on the map.")
        return True
    if cmd == "blueprint" and rest:
        blueprint_worksheet(rest[0])
        return True
    if cmd == "planned":
        planned()
        return True
    if cmd == "export":
        out = os.path.abspath(rest[0]) if rest else os.path.join(codemap.MAP, "..", "map-export.html")
        with open(out, "w") as f:
            f.write(export())
        print(f"Map exported to {os.path.normpath(out)}: one HTML file, flows and notes included, no health, no recent work, no folder paths.")
        return True
    return False
