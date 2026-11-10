"""hook.py's CLI commands that talk to the board: propose, listen, phone and snapshot. Part of hook.py, imported only when one runs."""
import json
import os
import secrets
import sys
import time

import hook
from hook import (
    alive, H, lan_ip, lan_token, LISTEN_HOURS, listening, locked, log, now, read_json, session, short, SKILL, split_title, take, VERSION,
    write_json)
from board_server import ensure_server, ping


def load_spec(args):
    """Parse `propose [file|-] [--revise ID]`: the proposal JSON, normalised."""
    revise = None
    if "--revise" in args:
        i = args.index("--revise")
        revise, args = args[i + 1], args[:i] + args[i + 2:]
    src = args[0] if args else "-"
    spec = json.load(sys.stdin if src == "-" else open(src))
    if not isinstance(spec, dict):
        raise ValueError("the proposal must be a JSON object")
    if not revise and not str(spec.get("title", "")).strip():
        raise ValueError('a new proposal needs a "title"')
    questions = []
    for i, q in enumerate(spec.get("questions") or [], 1):
        q = q if isinstance(q, dict) else {"text": str(q)}
        opts = [o if isinstance(o, dict) else {"label": str(o)} for o in q.get("options") or []]
        opts = [{"label": str(o.get("label", "")), "detail": str(o.get("detail", "")), "recommended": bool(o.get("recommended"))} for o in opts]
        kind = q.get("type") or ("one" if opts else "text")
        if kind not in ("one", "many", "text"):
            raise ValueError(f'question {i}: type must be "one", "many" or "text"')
        questions.append({"id": str(q.get("id") or f"q{i}"), "text": str(q.get("text", "")), "type": kind, "options": opts})
    preview = spec.get("preview") or ""
    if preview and not preview.lstrip().startswith("<"):
        preview = open(os.path.expanduser(preview)).read()  # a path to a self-contained HTML file
    if len(preview) > 2_000_000:
        raise ValueError("preview is over 2 MB; make it self-contained and smaller")
    return revise, {
        "title": str(spec.get("title", "")).strip(), "kind": str(spec.get("kind") or "feature"),
        "summary": str(spec.get("summary", "")), "body": str(spec.get("body", "")), "questions": questions,
        "tasks": [dict(zip(("title", "summary"), split_title(str(x)))) for x in spec.get("tasks") or []], "preview": preview,
        "map": __import__("mapapp").blueprint(spec.get("map")),
    }


def propose(d, b, sid, spec, revise, t):
    s = session(d, sid, t)
    s["updated"] = d["updated"] = t
    if revise:
        p = b["proposals"].get(revise)
        if not p:
            raise ValueError(f"no proposal {revise}")
        p["title"] = spec["title"] or p["title"]
        p["session"] = sid  # a resumed session can revise it
    else:
        b["seq"] += 1
        p = b["proposals"][f"p{b['seq']}"] = {"id": f"p{b['seq']}", "session": sid, "title": spec["title"], "kind": spec["kind"],
                                              "created": t, "versions": []}
    n = len(p["versions"]) + 1
    v = {"n": n, "at": t, **{k: spec[k] for k in ("summary", "body", "questions", "tasks")}}
    if spec.get("map"):
        v["map"] = spec["map"]
    if spec["preview"]:
        os.makedirs(os.path.join(hook.DIR, "previews"), exist_ok=True)
        v["preview"] = f"previews/{p['id']}-v{n}.html"
        with open(os.path.join(hook.DIR, v["preview"]), "w") as f:
            f.write(spec["preview"])
    p["versions"].append(v)
    p.update(status="awaiting", updated=t, session_title=s.get("title", ""))
    p.pop("delivered", None)
    if s.get("round") and p["id"] not in s["round"]["proposals"]:
        s["round"]["proposals"].append(p["id"])
    log(s, t, f"{'Revised' if revise else 'Proposed'} on the board: {short(p['title'], 80)}", "plan")
    return p["id"]


def listen(sid):
    """Block until the user sends something from the board for this session, then print it."""
    pid, end = os.getpid(), time.time() + LISTEN_HOURS * 3600
    with locked() as (d, b):
        other = b["listeners"].get(sid)
        if other and other.get("pid") != pid and alive(other.get("pid")) and listening(b, sid):
            print(f"A board listener is already running for this session (pid {other['pid']}). Nothing to do.")
            return
        b["listeners"][sid] = {"pid": pid, "at": now()}
    seen, beat = None, time.time()
    while time.time() < end:
        mtime = os.path.getmtime(hook.BOARD) if os.path.exists(hook.BOARD) else 0
        if mtime != seen or time.time() - beat > 30:
            with locked() as (d, b):
                if d["sessions"].get(sid, {}).get("status") == "ended":
                    b["listeners"].pop(sid, None)
                    return
                msg = take(d, b, sid, now(), True)
                if msg:
                    b["listeners"].pop(sid, None)
                else:
                    b["listeners"][sid] = {"pid": pid, "at": now()}
            if msg:
                print(msg)
                return
            seen, beat = os.path.getmtime(hook.BOARD) if os.path.exists(hook.BOARD) else 0, time.time()
        time.sleep(1)
    with locked() as (d, b):
        b["listeners"].pop(sid, None)
    print(f"Nothing came from the board in {LISTEN_HOURS} hours, so the listener stopped. Start it again if the user is still around.")


def phone(on):
    path = os.path.join(hook.DIR, "lan.json")
    if on:
        write_json(path, {"token": lan_token() or secrets.token_urlsafe(8)})
        os.chmod(path, 0o600)
    elif os.path.exists(path):
        os.remove(path)
    ensure_server()
    for _ in range(40):  # wait for the restarted server
        if ping() == (VERSION, on):
            break
        time.sleep(0.1)
    if not on:
        return print(f"Phone access is off. The board is back to this computer only: http://localhost:{hook.PORT}")
    ip = lan_ip()
    if not ip:
        return print("Couldn't find this computer's Wi-Fi address. Is it on a network?")
    print(f"Phone access is on. Open this on a phone on the same Wi-Fi (the key is in the link; it's kept as a cookie after the first visit):\n"
          f"http://{ip}:{hook.PORT}/?key={lan_token()}\n"
          f"Anyone with this link on your network can read the board and send Claude requests. Turn it off with `{H} phone off`.")


def snapshot(out):
    """The page with all its data baked in: read-only, no server needed (for a claude.ai page or a file)."""
    with locked() as (d, b):
        snap = {"progress": d, "board": {**b, "listeners": {}, "requests": []}, "at": now(),
                "history": open(hook.HIST).read() if os.path.exists(hook.HIST) else "", "map": read_json(os.path.join(hook.DIR, "map", "graph.json"), None)}
    html = open(os.path.join(SKILL, "index.html")).read()
    blob = json.dumps(snap).replace("</", "<\\/")
    marker = "<script>\nconst $ ="
    if marker not in html:
        raise ValueError("index.html changed: snapshot marker not found")
    html = html.replace(marker, f"<script>window.SNAPSHOT = {blob};</script>\n{marker}", 1)
    with open(out, "w") as f:
        f.write(html)
    print(f"Snapshot written to {out} ({len(html) // 1024} KB). It contains prompts, plans, replies and file paths from this project.")
