"""The board's web servers: starting them, the project board (serve and its /api handlers) and the all-projects hub.
Part of hook.py, which imports it only when a server starts or the board is opened to phones."""
import fcntl
import glob
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import hook
from hook import (
    age, clip, fresh, HUB_DIR, HUB_PORT, lan_ip, lan_token, locked, log, MAX_LOG, MAX_REQUESTS, now, OPEN, read_json, REG, send_push,
    short, SKILL, sweep, update_info, VERSION, write_json)


def ping():
    """What the server on our port reports: (version, phone access on), or None if it isn't ours."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{hook.PORT}/api/ping", timeout=0.5) as r:
            j = json.loads(r.read())
            return j.get("v"), bool(j.get("lan"))
    except Exception:
        return None


def port_open(port=None):
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port or hook.PORT)) == 0


def stop_ours(port, markers):
    """Stop the process listening on the port if it's one of ours (an older board or hub). True if the port is free now."""
    try:
        pids = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.split()
        for pid in pids:
            cmd = subprocess.run(["ps", "-o", "command=", "-p", pid], capture_output=True, text=True).stdout
            if any(m in cmd for m in markers):
                os.kill(int(pid), 15)
        for _ in range(20):
            if not port_open(port):
                break
            time.sleep(0.1)
    except Exception:
        pass
    return not port_open(port)


def register(add=True):
    """Add this project to (or remove it from) the hub's registry; projects whose .progress/ is gone are dropped."""
    os.makedirs(HUB_DIR, exist_ok=True)
    with open(os.path.join(HUB_DIR, ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reg = read_json(REG, {})
        reg = reg if isinstance(reg, dict) else {}
        ps, before = reg.setdefault("projects", {}), json.dumps(reg, sort_keys=True)
        root = os.path.abspath(hook.ROOT)
        if add:
            ps[root] = {"name": os.path.basename(root), "port": hook.PORT, "added": (ps.get(root) or {}).get("added") or now()}
        else:
            ps.pop(root, None)
        for gone in [k for k in ps if not os.path.isdir(os.path.join(k, ".progress"))]:
            ps.pop(gone)
        if json.dumps(reg, sort_keys=True) != before:
            write_json(REG, reg)


def touch(path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
    except OSError:
        pass


def write_launcher():
    """~/.claude/project-plan/hook.py runs whichever copy is installed, so commands in rules and docs never change path."""
    me = os.path.realpath(hook.__file__)
    body = (f"#!/usr/bin/env python3\n# project-plan launcher, rewritten at every session start. Runs the installed copy:\n"
            f"import os, sys\nHOOK = {me!r}\nos.execv(sys.executable, [sys.executable, HOOK] + sys.argv[1:])\n")
    path = os.path.join(HUB_DIR, "hook.py")
    try:
        if not os.path.exists(path) or open(path).read() != body:
            os.makedirs(HUB_DIR, exist_ok=True)
            with open(path + ".tmp", "w") as f:
                f.write(body)
            os.replace(path + ".tmp", path)
    except OSError:
        pass


def ensure_hub():
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{HUB_PORT}/api/ping", timeout=0.5) as r:
            if json.loads(r.read()).get("v") == VERSION:
                return
    except Exception:
        pass
    if port_open(HUB_PORT) and not stop_ours(HUB_PORT, ["hook.py hub"]):
        return  # something else owns the port: no hub
    os.makedirs(HUB_DIR, exist_ok=True)
    subprocess.Popen([sys.executable, os.path.abspath(hook.__file__), "hub"], cwd=HUB_DIR,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def ensure_server():
    """Keep the page in step with the skill's copy, and our server (current version) on the port."""
    for page in ("index.html", "map.html"):
        src, dst = os.path.join(SKILL, page), os.path.join(hook.DIR, page)
        if os.path.exists(src) and (not os.path.exists(dst) or open(src, "rb").read() != open(dst, "rb").read()):
            shutil.copyfile(src, dst)
    try:
        write_launcher()
        register()
        ensure_hub()
    except Exception:
        pass  # the hub is extra: never let it stop the project's board
    if ping() == (VERSION, bool(lan_token())):
        return
    if port_open() and not stop_ours(hook.PORT, ["http.server", "hook.py serve"]):
        return  # something else owns the port: leave it alone
    subprocess.Popen([sys.executable, os.path.abspath(hook.__file__), "serve"], cwd=hook.ROOT,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def api_respond(body):
    """The user's answer to a proposal: approve, ask for changes, or dismiss."""
    decision = body.get("decision")
    if decision not in ("approve", "revise", "dismiss"):
        raise ValueError("decision must be approve, revise or dismiss")
    with locked() as (d, b):
        p = b["proposals"].get(str(body.get("proposal")))
        if not p:
            return 404, {"error": "That proposal no longer exists."}
        v = p["versions"][-1]
        if v["n"] != body.get("version") or p["status"] != "awaiting":
            return 409, {"error": "This proposal changed or was already answered. The page will refresh."}
        given = body.get("answers") if isinstance(body.get("answers"), dict) else {}
        answers = {}
        for q in v["questions"]:
            a = given.get(q["id"])
            answers[q["id"]] = [clip(x, 300) for x in a if isinstance(x, str)][:30] if q["type"] == "many" and isinstance(a, list) else clip(a, 4000)
        t = now()
        v["response"] = {"at": t, "decision": decision, "answers": answers, "instructions": clip(body.get("instructions"), 8000)}
        p["status"] = {"approve": "approved", "revise": "revise", "dismiss": "dismissed"}[decision]
        p["updated"] = t
        p.pop("delivered", None)
        s = d["sessions"].get(p["session"])
        if s:
            what = {"approve": "approved", "revise": "asked for changes to", "dismiss": "dismissed"}[decision]
            s["log"] = (s.get("log", []) + [{"at": t, "text": f"You {what} “{short(p['title'], 80)}” on the board", "kind": "you"}])[-MAX_LOG:]
    return 200, {"ok": True}


def api_request(body):
    """A new request typed on the board: brainstorm, suggest features, or do a task."""
    kind, text = body.get("kind"), clip(body.get("text"), 8000)
    if kind not in ("brainstorm", "suggest", "task"):
        raise ValueError("kind must be brainstorm, suggest or task")
    if not text:
        raise ValueError("Write what you want first.")
    with locked() as (d, b):
        b["seq"] += 1
        b["requests"].append({"id": f"r{b['seq']}", "kind": kind, "text": text, "instructions": clip(body.get("instructions"), 8000),
                              "session": clip(body.get("session"), 100), "at": now()})
        b["requests"] = b["requests"][-MAX_REQUESTS:]
    return 200, {"ok": True}


def api_permit(body):
    """Allow or deny a waiting permission request from the board (the hook is polling for it)."""
    decision = body.get("decision")
    if decision not in ("allow", "deny"):
        raise ValueError("decision must be allow or deny")
    with locked() as (d, b):
        s = d["sessions"].get(str(body.get("session")))
        n = (s or {}).get("needs") or {}
        if not n.get("id") or n["id"] != body.get("id") or age(n.get("until")) > 0:
            return 409, {"error": "That request was already answered, or it's now waiting in the terminal."}
        t = now()
        b["perms"] = {k: v for k, v in b["perms"].items() if age(v.get("at")) < 3600}
        b["perms"][n["id"]] = {"decision": decision, "at": t}
        s.pop("needs", None)
        log(s, t, f"You {'allowed' if decision == 'allow' else 'denied'} on the board: {short(n.get('what'), 120)}", "you")
    return 200, {"ok": True}


def api_settings(body):
    """Daily spend budget ($) and context limit (%); 0 turns a limit off."""
    def num(k, hi):
        try:
            return max(0.0, min(float(body.get(k) or 0), hi))
        except (TypeError, ValueError):
            raise ValueError(f"{k} must be a number")
    topic, server = clip(body.get("ntfy_topic"), 64), clip(body.get("ntfy_server"), 200).rstrip("/")
    if topic and not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", topic):
        raise ValueError("The ntfy topic needs 6–64 letters, digits, - or _.")
    if server and not re.fullmatch(r"https://[^\s/]+(/[^\s]*)?", server):
        raise ValueError("The ntfy server must be an https:// address.")
    with locked() as (d, b):
        b["settings"] = {"budget_day": round(num("budget_day", 100000), 2), "ctx_limit": int(num("ctx_limit", 100)),
                         "keep_days": int(num("keep_days", 3650)), "ntfy_topic": topic, "ntfy_server": server}
    return 200, {"ok": True}


def api_pushtest(body):
    st = read_json(hook.BOARD, {}).get("settings") or {}
    if not st.get("ntfy_topic"):
        raise ValueError("Save a topic first.")
    try:
        send_push(st, "Project board", f"Test alert from the {os.path.basename(hook.ROOT)} board. Alerts arrive here when you're away from the board.")
    except Exception as e:
        return 502, {"error": f"ntfy didn't accept it: {e}"}
    return 200, {"ok": True}


def api_rename(body):
    with locked() as (d, b):
        s = d["sessions"].get(str(body.get("session")))
        if not s:
            return 404, {"error": "That session is gone."}
        name = " ".join(clip(body.get("name"), 80).split())
        if name:
            s["name"] = name
        else:
            s.pop("name", None)
    return 200, {"ok": True}


def api_git():
    """Uncommitted files in the project (paths relative to the project), so the board can show what still needs a commit."""
    git = lambda *a: subprocess.run(["git", "-C", hook.ROOT, *a], capture_output=True, text=True, timeout=5)
    try:
        if git("rev-parse", "--git-dir").returncode:
            return 200, {"repo": False, "files": {}}
        prefix = git("rev-parse", "--show-prefix").stdout.strip()
        out = git("status", "--porcelain=v1", "-uall", "--", ".").stdout
    except (OSError, subprocess.SubprocessError) as e:
        return 200, {"repo": False, "files": {}, "error": str(e)}
    files = {}
    for line in out.splitlines():
        path = line[3:].split(" -> ")[-1].strip('"')
        files[path[len(prefix):] if path.startswith(prefix) else path] = line[:2].strip() or "M"
    return 200, {"repo": True, "files": files}


def api_note(body):
    """Pin a finished request in History, or add a note to it."""
    hid = clip(body.get("id"), 200)
    if not hid:
        raise ValueError("id is required")
    with locked() as (d, b):
        n = b["notes"].get(hid, {})
        if "pin" in body:
            n["pin"] = bool(body["pin"])
        if "note" in body:
            n["note"] = clip(body.get("note"), 4000)
        n["at"] = now()
        if n.get("pin") or n.get("note"):
            b["notes"][hid] = n
        else:
            b["notes"].pop(hid, None)
    return 200, {"ok": True}


def api_diff(path, whole=False):
    """git diff of one project file against HEAD (untracked files: the whole file as added)."""
    root = os.path.realpath(hook.ROOT)
    full = os.path.realpath(os.path.join(root, path or ""))
    if not path or path.startswith("…") or not full.startswith(root + os.sep):
        return 400, {"error": "Only files inside the project have a diff."}
    git = lambda *a: subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, timeout=5)
    full = os.path.relpath(full, root)  # git paths relative to the project, so the diff header reads like the board
    try:
        if git("rev-parse", "--git-dir").returncode:
            return 200, {"diff": "", "note": "This project isn't a git repository, so there's nothing to diff against."}
        out = git("diff", "--no-color", "--no-ext-diff", "-U100000" if whole else "-U3", "HEAD", "--", full).stdout
        if not out and git("ls-files", "--error-unmatch", "--", full).returncode and os.path.isfile(os.path.join(root, full)):
            out = git("diff", "--no-color", "--no-index", "--", "/dev/null", full).stdout  # new, untracked file
    except (OSError, subprocess.SubprocessError) as e:
        return 200, {"diff": "", "note": f"git failed: {e}"}
    return 200, {"diff": out[:300_000], "cut": len(out) > 300_000, "note": "" if out else "No uncommitted changes: it's identical to the last commit."}


TE_BIN = next((p for p in (os.path.expanduser("~/.tokeneconomizer/bin/te"),  # made by the plugin's first hook
                            *glob.glob(os.path.expanduser("~/.claude/plugins/cache/tokeneconomizer/*/*/scripts/te")),
                            os.path.expanduser("~/TokenEconomizer/plugin/scripts/te")) if os.path.exists(p)),
              os.path.expanduser("~/.tokeneconomizer/bin/te"))
TE_CONFIG = {"microskills": ("on", "off", "envfacts"), "toolprune": ("propose", "auto", "off"), "loopguard": ("nudge", "off")}


def te(*a, timeout=30):
    """Run TokenEconomizer's `te` for this project (Claude Code host); (ok, stdout)."""
    if not os.path.exists(TE_BIN):
        return False, "TokenEconomizer isn't installed (no ~/.tokeneconomizer/bin/te yet)."
    try:
        r = subprocess.run([TE_BIN, "--host", "claude", "--cwd", hook.ROOT, *a], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout or r.stderr).strip()


def api_te_status():
    """What TokenEconomizer knows and did here: `te status --json`, its facts and its per-session ledger."""
    ok, out = te("status", "--json")
    if not ok:
        return 200, {"installed": os.path.exists(TE_BIN), "error": out}
    try:
        st = json.loads(out)
    except ValueError:
        return 200, {"installed": True, "error": "te status didn't return JSON: " + out[:300]}
    base = st.get("state") or ""
    inj = read_json(os.path.join(base, "injection.json"), {}) if base else {}
    facts = [{"key": k, **{f: v.get(f) for f in ("state", "fire", "inject", "high", "drift")}}
             for k, v in (inj.get("facts") or {}).items() if isinstance(v, dict) and "state" in v]
    rows = []
    try:
        with open(os.path.join(base, "ledger.jsonl")) as f:
            for line in f.readlines()[-400:]:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                rows.append({k: r.get(k) for k in ("kind", "session_id", "at", "imported", "turns", "nudges", "aborts",
                                                   "injected_chars", "effective_tokens", "untool_applied", "overhead", "requests")})
    except OSError:
        pass
    return 200, {"installed": True, "status": st, "facts": facts, "rendered": inj.get("rendered") or "", "ledger": rows}


def api_te(body):
    """Steer TokenEconomizer from the board: apply/restore pruned tools, or set its config."""
    act = body.get("action")
    if act in ("apply", "restore"):
        tools = [t for t in body.get("tools") or [] if isinstance(t, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_:.-]{0,79}", t)]
        ok, out = te("toolprune", act, *tools, timeout=60)
    elif act == "config":
        pairs = [f"{k}={v}" for k, v in (body.get("set") or {}).items() if v in TE_CONFIG.get(k, ())]
        if not pairs:
            raise ValueError("Nothing valid to set.")
        ok, out = te("config", "set", "--global", *pairs)
    else:
        raise ValueError("Unknown action.")
    return (200 if ok else 400), ({"ok": True, "out": out} if ok else {"error": out})


MAP_LOCK = threading.Lock()


def rebuild_map():
    """Rebuild the map in a fresh process, since this server may have loaded the map code before it was updated. An error line, or ""."""
    r = subprocess.run([sys.executable, os.path.abspath(hook.__file__), "map", "update", "--quiet"], cwd=hook.ROOT,
                       capture_output=True, text=True, timeout=600)
    return (r.stderr.strip().splitlines() or ["The map rebuild failed."])[-1][:300] if r.returncode else ""


def api_map(path, body):
    """The Map view: rescan, dismiss a health issue, notes, Ask, flows on request, and blueprint undo."""
    import codemap, mapapp
    codemap.setup(hook.ROOT, hook.DIR)
    if path == "/api/map/rescan":
        with MAP_LOCK:
            err = rebuild_map()
        return (500, {"error": err}) if err else (200, {"ok": True})
    if path == "/api/map/dismiss":
        f = os.path.join(hook.DIR, "map", "dismissed.json")
        with MAP_LOCK:
            d = read_json(f, {})
            if body.get("on", True):
                d[clip(body.get("id"), 200)] = {"at": now(), "reason": clip(body.get("reason"), 500)}
            else:
                d.pop(str(body.get("id")), None)
            write_json(f, d)
            err = rebuild_map()
        return (500, {"error": err}) if err else (200, {"ok": True})
    if path == "/api/map/note":
        p = clip(body.get("path"), 500)
        if not p:
            raise ValueError("Which file?")
        with MAP_LOCK:
            mapapp.note(p, clip(body.get("text"), 4000))
        return 200, {"ok": True}
    if path == "/api/map/undo":
        with MAP_LOCK:
            mapapp.undo()
        return 200, {"ok": True}
    q = clip(body.get("q"), 2000)
    if not q:
        raise ValueError("Write a question first.")
    if not ASK_LOCK.acquire(blocking=False):
        return 409, {"error": "Claude is still answering the last question; try again when it's done."}
    try:
        if path == "/api/map/flow":
            r = mapapp.flow(q)
        else:
            ctx = [clip(x, 400) for x in body.get("context") or [] if isinstance(x, str)][:10]
            r = mapapp.ask(q, mapapp.asks()[-3:] if body.get("followup") else [], ctx, "change" if body.get("mode") == "change" else "ask")
        return (502, {"error": r["error"]}) if r.get("error") else (200, r)
    finally:
        ASK_LOCK.release()


ASK_LOCK = threading.Lock()


def serve():
    import functools
    from http.cookies import SimpleCookie
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlsplit
    last_sweep = [0]
    token = lan_token()  # phone access: listen on the Wi-Fi too, but every non-local request needs the key

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            if self.path.startswith("/previews/"):  # mockups run sandboxed, even when opened in their own tab
                self.send_header("Content-Security-Policy", "sandbox allow-scripts")
            super().end_headers()

        def local(self):
            return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

        def allowed(self):
            if self.local():  # and addressed to localhost, which blocks DNS rebinding
                return (self.headers.get("Host") or "").rsplit(":", 1)[0] in ("localhost", "127.0.0.1")
            c = SimpleCookie(self.headers.get("Cookie") or "")
            return bool(token) and "pp" in c and hmac.compare_digest(c["pp"].value, token)

        def send(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlsplit(self.path)
            key = parse_qs(url.query).get("key", [""])[0]
            if token and key and not self.local() and hmac.compare_digest(key, token):
                self.send_response(303)  # swap the key in the link for a cookie, and drop it from the address bar
                self.send_header("Set-Cookie", f"pp={token}; Max-Age=2592000; HttpOnly; SameSite=Strict; Path=/")
                self.send_header("Location", url.path or "/")
                return self.end_headers()
            if not self.allowed():
                return self.send_error(403, "Open the board with the link (including its key) from `hook.py phone on`.")
            name = os.path.basename(url.path)
            if name.startswith(".") or name == "lan.json" or name.endswith(".tmp"):
                return self.send_error(404)
            if url.path == "/api/ping":
                return self.send(200, {"app": "project-plan", "v": VERSION, "lan": bool(token),
                                       "hub": fresh(".hub-open", 90), "update": update_info() if self.local() else None, "hub_url": f"http://localhost:{HUB_PORT}" if self.local() and port_open(HUB_PORT) else ""})
            if url.path == "/api/git":
                return self.send(*api_git())
            if url.path == "/api/te":
                return self.send(*api_te_status())
            if url.path == "/progress.json" and time.time() - last_sweep[0] > 15:  # end sessions whose Claude exited
                last_sweep[0] = time.time()
                try:
                    with locked() as (d, b):
                        sweep(d, b, now())
                except Exception:
                    pass
            if url.path == "/api/map/export":
                import codemap, mapapp
                codemap.setup(hook.ROOT, hook.DIR)
                o = parse_qs(url.query)
                on = lambda k, d: o.get(k, [d])[0] == "1"
                hist = open(hook.HIST).read() if on("recent", "0") and os.path.exists(hook.HIST) else ""
                try:
                    data = mapapp.export(on("flows", "1"), on("notes", "1"), on("recent", "0"), on("health", "0"), hist).encode()
                except (OSError, ValueError) as e:
                    return self.send(400, {"error": str(e)})
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(hook.ROOT)}-map.html"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            if url.path == "/api/security":
                import secaudit
                return self.send(200, secaudit.view(hook.ROOT, os.path.join(hook.DIR, "map"), parse_qs(url.query).get("run", [""])[0]))
            if url.path == "/api/diff":
                q = parse_qs(url.query)
                return self.send(*api_diff(q.get("path", [""])[0], q.get("full", [""])[0] == "1"))
            if url.path == "/progress.json" and "seen" in url.query:  # a visible board tab: permission requests wait for it
                try:
                    open(os.path.join(hook.DIR, ".seen"), "w").close()
                except OSError:
                    pass
            if url.path == "/api/lan":
                ip = lan_ip() if token else None
                return self.send(200, {"on": bool(token), "url": f"http://{ip}:{hook.PORT}/?key={token}" if self.local() and ip else ""})
            super().do_GET()

        def do_POST(self):
            # Same-origin JSON only: other websites can't post into Claude's session.
            if not self.allowed() or self.headers.get("Origin") != f"http://{self.headers.get('Host')}" \
                    or not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self.send(403, {"error": "Forbidden"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > 200_000:
                return self.send(413, {"error": "Too large"})
            route = {"/api/respond": api_respond, "/api/request": api_request, "/api/permit": api_permit,
                     "/api/settings": api_settings, "/api/note": api_note,
                     "/api/rename": api_rename, "/api/pushtest": api_pushtest, "/api/te": api_te,
                     **{p: functools.partial(api_map, p) for p in ("/api/map/rescan", "/api/map/dismiss", "/api/map/ask", "/api/map/note",
                                                                    "/api/map/flow", "/api/map/undo")}}.get(self.path)
            if not route:
                return self.send(404, {"error": "Not found"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("Expected a JSON object")
                self.send(*route(body))
            except (ValueError, TypeError, KeyError) as e:
                self.send(400, {"error": str(e)})

    ThreadingHTTPServer(("0.0.0.0" if token else "127.0.0.1", hook.PORT), functools.partial(Handler, directory=hook.DIR)).serve_forever()


# ---------------------------------------------------------------- the all-projects hub

def local_day(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def project_summary(path, meta):
    """What the hub shows for one project, read straight from its .progress/ files (the hub never writes them)."""
    d, b = read_json(os.path.join(path, ".progress", "progress.json"), {}), read_json(os.path.join(path, ".progress", "board.json"), {})
    d, b = (d if isinstance(d, dict) else {}), (b if isinstance(b, dict) else {})
    day, port = datetime.now().strftime("%Y-%m-%d"), meta.get("port")
    try:
        port = int(open(os.path.join(path, ".progress", "port")).read().strip())
    except (OSError, ValueError):
        pass
    ss = [s for s in (d.get("sessions") or {}).values() if isinstance(s, dict)]
    live = [s for s in ss if s.get("status") in ("working", "active", "idle")]
    name = lambda s: s.get("name") or re.sub(r"<task-notification>.*", "Background task finished", s.get("title") or "", flags=re.S) or "New session"
    def now_text(s):
        cur = next((t for t in s.get("tasks", []) if t.get("status") == "in_progress"), None)
        return (cur.get("active") or cur.get("title")) if cur else ("Finished its last reply" if s.get("status") == "idle" else ((s.get("log") or [{}])[-1].get("text") or "Starting"))
    return {
        "path": path, "short": path.replace(os.path.expanduser("~"), "~", 1), "name": meta.get("name") or os.path.basename(path), "port": port, "up": bool(port) and port_open(port),
        "updated": d.get("updated") or "",
        "sessions": [{"id": s["id"], "title": name(s), "status": s.get("status"), "now": short(now_text(s), 120), "updated": s.get("updated"),
                      "done": sum(t.get("status") == "done" for t in s.get("tasks", [])), "total": len(s.get("tasks", []))}
                     for s in sorted(live, key=lambda s: s.get("updated", ""), reverse=True)],
        "needs": [{"sid": s["id"], "title": name(s), **{k: s["needs"].get(k) for k in ("what", "at", "id", "until")}} for s in live if s.get("needs")],
        "proposals": [{"id": p.get("id"), "title": p.get("title"), "updated": p.get("updated")} for p in (b.get("proposals") or {}).values()
                      if isinstance(p, dict) and p.get("status") == "awaiting"],
        "open": sum(t.get("status") in OPEN for s in live for t in s.get("tasks", [])),
        "shipped": sum(t.get("status") == "done" and local_day(t.get("finished")) == day for s in ss for t in s.get("tasks", [])),
        "spend": round(sum((s.get("usage") or {}).get("days", {}).get(day, 0) for s in ss), 4),
        "failing": [s["check"].get("name") for s in live if isinstance(s.get("check"), dict) and not s["check"].get("ok")],
        "map": map_summary(path),
    }


def map_summary(path):
    """The project map's headline numbers for the hub (None when the project has no map yet)."""
    g = read_json(os.path.join(path, ".progress", "map", "graph.json"), None)
    if not isinstance(g, dict) or not isinstance(g.get("parts"), list):
        return None
    h = g.get("health") or {}
    return {"parts": len(g["parts"]), "auto": bool(g.get("auto")), "blueprint": bool(g.get("blueprint")), "stale": len(g.get("stale") or []),
            "high": h.get("high", 0), "medium": h.get("medium", 0), "scanned": g.get("scanned", "")}


def hub():
    """A local page listing every project with a board. Read-only, except Allow / Deny, which goes through the project's own server."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlsplit
    import urllib.request, urllib.error

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def allowed(self):
            return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1") \
                and (self.headers.get("Host") or "").rsplit(":", 1)[0] in ("localhost", "127.0.0.1")

        def send(self, code, obj, ctype="application/json"):
            data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.allowed():
                return self.send(403, {"error": "Forbidden"})
            url = urlsplit(self.path)
            if url.path in ("/", "/index.html"):
                return self.send(200, open(os.path.join(SKILL, "hub.html"), "rb").read(), "text/html; charset=utf-8")
            if url.path == "/api/ping":
                return self.send(200, {"app": "project-plan-hub", "v": VERSION})
            if url.path == "/api/projects":
                touch(os.path.join(HUB_DIR, ".hub-open"))  # project tabs stay quiet while the hub is open
                if "seen" in url.query:
                    touch(os.path.join(HUB_DIR, ".hub-seen"))  # and permission requests wait for it while it's visible
                out = []
                for path, meta in (read_json(REG, {}).get("projects") or {}).items():
                    if os.path.isdir(os.path.join(path, ".progress")):
                        try:
                            out.append(project_summary(path, meta if isinstance(meta, dict) else {}))
                        except Exception:
                            pass
                return self.send(200, {"projects": out, "at": now()})
            self.send(404, {"error": "Not found"})

        def do_POST(self):
            if not self.allowed() or self.headers.get("Origin") != f"http://{self.headers.get('Host')}" \
                    or not (self.headers.get("Content-Type") or "").startswith("application/json") or self.path != "/api/permit":
                return self.send(403, {"error": "Forbidden"})
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 20_000)) or b"{}")
                path = str(body.get("project"))
                meta = (read_json(REG, {}).get("projects") or {}).get(path)  # only registered projects, never a path from the request
                if not isinstance(meta, dict):
                    return self.send(404, {"error": "That project isn't registered."})
                port = project_summary(path, meta)["port"]
                fwd = {k: body.get(k) for k in ("session", "id", "decision")}
                req = urllib.request.Request(f"http://localhost:{port}/api/permit", json.dumps(fwd).encode(),
                                             {"Content-Type": "application/json", "Origin": f"http://localhost:{port}"})
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return self.send(r.status, json.loads(r.read()))
                except urllib.error.HTTPError as e:
                    return self.send(e.code, json.loads(e.read() or b"{}"))
            except (ValueError, TypeError, KeyError, OSError) as e:
                self.send(502, {"error": f"Couldn't reach that project's board: {e}"})

    ThreadingHTTPServer(("127.0.0.1", HUB_PORT), Handler).serve_forever()
