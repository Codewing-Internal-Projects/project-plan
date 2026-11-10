#!/usr/bin/env python3
"""project-plan: hook, CLI and board server for one project's .progress/ folder.

As a hook (no arguments) it reads the event from stdin and mirrors the session into
.progress/progress.json. It never blocks or fails the session: errors are swallowed.

CLI, run from inside the project by Claude:
  hook.py plan "Title — summary" ...     replace the plan (sessions without a task tool)
  hook.py add "Title — summary"          append a task
  hook.py task <n> <status>              pending|in_progress|done|blocked (1-based)
  hook.py propose [file|-] [--revise ID] post a proposal (JSON) for the user to answer on the board
  hook.py proposal <ID> <status>         building|done|dismissed|awaiting
  hook.py listen                         wait until the user sends something from the board, print it
  hook.py phone on|off                   open the board to phones on the same Wi-Fi (with a key), or close it
  hook.py snapshot [out.html]            a self-contained, read-only copy of the board (to publish or share)
  hook.py serve                          the board's web server (started automatically)
  hook.py push <title> <message>         send a phone alert through ntfy (used by the hooks)
  hook.py hub                            the all-projects hub on port 4320 (started automatically)

Files in .progress/: progress.json (live sessions), board.json (proposals, requests,
listeners), history.jsonl (one line per finished request), previews/ (proposal mockups),
lan.json (phone access key, never served). All writes happen under .progress/.lock.
"""
import glob
import contextlib, fcntl, hashlib, hmac, json, os, re, secrets, shutil, socket, subprocess, sys, threading, time
from datetime import datetime, timezone

SKILL = os.path.dirname(os.path.realpath(__file__))
# Installed as a plugin, plugin.json sits three folders up (plugins/project-plan/skills/project-plan/scripts).
PLUGIN_JSON = os.path.join(SKILL, "..", "..", "..", ".claude-plugin", "plugin.json")
try:
    _meta = json.load(open(PLUGIN_JSON))
    IS_PLUGIN, APP_VERSION = _meta.get("name") == "project-plan", str(_meta.get("version") or "")
except (OSError, ValueError):
    IS_PLUGIN, APP_VERSION = False, ""
REPO = "Codewing-Internal-Projects/project-plan"  # where updates come from
VERSION = 13  # bump when the server changes, so running boards restart with the new code

# $ per million tokens: (model id fragment, input, output, cache read). Cache writes cost 1.25x input (5 min) or 2x (1 h).
# ponytail: prices as of 2026-10-06 (claude-api skill); an unknown model counts as $0, add it here when prices change
PRICES = [
    ("claude-fable-5", 10, 50, 0.25), ("claude-mythos-5", 10, 50, 0.25),
    ("claude-opus-5-5", 4, 20, 0.20), ("claude-opus-5", 5, 25, 0.50), ("claude-opus-4", 5, 25, 0.50),
    ("claude-sonnet-5", 2, 10, 0.20), ("claude-sonnet-4", 3, 15, 0.30),
    ("claude-haiku-5", 0.10, 0.50, 0.01), ("claude-haiku-4", 1, 5, 0.10),
]
MAX_LOG, MAX_SESSIONS, MAX_FILES, MAX_REQUESTS = 80, 25, 40, 200
LISTEN_HOURS = 24
PERMIT_WAIT = 40  # seconds a permission request waits for an answer on the board (if the board is open) before the terminal asks
H = "python3 ~/.claude/project-plan/hook.py"  # a stable launcher, rewritten at session start to point at this copy
ROOT = DIR = DATA = LOCK = BOARD = HIST = None
PORT = 4321
HUB_PORT = 4320
HUB_DIR = os.path.expanduser("~/.claude/project-plan")  # the registry of projects with a board, and hub heartbeats
REG = os.path.join(HUB_DIR, "projects.json")

LISTEN_HINT = (
    f"Board listener: start `{H} listen` in the background now (Bash with run_in_background) unless it is already running. "
    "It exits when the user sends something from the board (an answer to a proposal, or a new request) and prints what they sent: "
    "act on it, then start the listener again."
)


def find_project(start):
    """The project root: the nearest folder (from start upwards) that has .progress/."""
    d = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(d, ".progress")):
            return d
        if os.path.dirname(d) == d:
            return None
        d = os.path.dirname(d)


def use_project(root):
    global ROOT, DIR, DATA, LOCK, BOARD, HIST, PORT
    ROOT, DIR = root, os.path.join(root, ".progress")
    DATA, LOCK = os.path.join(DIR, "progress.json"), os.path.join(DIR, ".lock")
    BOARD, HIST = os.path.join(DIR, "board.json"), os.path.join(DIR, "history.jsonl")
    try:
        PORT = int(open(os.path.join(DIR, "port")).read().strip())
    except (OSError, ValueError):
        PORT = 4321


def reminder():
    return (
        f"Progress board: the user follows this session live at http://localhost:{PORT}. "
        "Before you start on this request, write a task plan with your task list tool so they can see what is planned, "
        "what is being done and what is left. Give every task a short title and a one-line summary: "
        "with TodoWrite write each item as 'Title — summary'; with TaskCreate use subject for the title and description for the summary. "
        "Mark tasks in_progress when you start them and completed as soon as they are done, and add tasks if the plan changes. "
        "Skip the plan only for a one-step answer that needs no tools. "
        f"If the user asks you to brainstorm, suggest features or choose between approaches, post a proposal on the board with `{H} propose` "
        "instead of asking in chat, and build only once they approve it there."
    )


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def age(iso):
    try:
        return (datetime.now(timezone.utc) - datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def short(s, n=90):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def clip(s, n):
    return str(s).strip()[:n] if isinstance(s, str) else ""


def rel(p):
    if not p or not os.path.isabs(p):
        return p
    r = os.path.relpath(p, ROOT)
    return "…/" + os.path.basename(p) if r.startswith("..") else r  # outside the project: just the name


def window(model):
    """Context window in tokens: 1M on current models, 200K on older ones."""
    m = model or ""
    return 1_000_000 if "[1m]" in m or re.search(r"fable|mythos|opus-5|opus-4-[6-9]|sonnet-5|sonnet-4-6|haiku-5", m) else 200_000


def cost_of(model, u):
    p = next((p[1:] for p in PRICES if p[0] in (model or "")), None)
    if not p:
        return 0.0
    i, o, cr = p
    w1h = (u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0
    w5m = max((u.get("cache_creation_input_tokens") or 0) - w1h, 0)
    return ((u.get("input_tokens") or 0) * i + (u.get("output_tokens") or 0) * o
            + (u.get("cache_read_input_tokens") or 0) * cr + w5m * i * 1.25 + w1h * i * 2) / 1e6


def read_usage(path, off=0, last=None):
    """Assistant usage written to a transcript since byte `off`: (new offset, last message id, [(model, usage)]).

    A message spans several lines that repeat the same usage, so each message id counts once."""
    out = []
    try:
        size = os.path.getsize(path)
        if size < off:
            off, last = 0, None  # the transcript was rewritten
        with open(path, "rb") as f:
            f.seek(off)
            chunk = f.read(size - off)
    except OSError:
        return off, last, out
    end = chunk.rfind(b"\n") + 1  # only whole lines; the rest is read next time
    for line in chunk[:end].splitlines():
        if b'"usage"' not in line:
            continue
        try:
            j = json.loads(line)
        except ValueError:
            continue
        m = j.get("message") if j.get("type") == "assistant" else None
        if isinstance(m, dict) and isinstance(m.get("usage"), dict) and m.get("id") != last:
            last = m.get("id")
            out.append((m.get("model") or "", m["usage"]))
    return off + end, last, out


def add_usage(s, msgs, sub=None):
    """Add tokens and cost to the session (and today's total); the main thread also sets context fill."""
    if not msgs:
        return
    u = s.setdefault("usage", {"in": 0, "out": 0, "cr": 0, "cw": 0, "cost": 0.0, "days": {}})
    day = datetime.now().strftime("%Y-%m-%d")
    for model, x in msgs:
        c = cost_of(model, x)
        u["in"] += x.get("input_tokens") or 0
        u["out"] += x.get("output_tokens") or 0
        u["cr"] += x.get("cache_read_input_tokens") or 0
        u["cw"] += x.get("cache_creation_input_tokens") or 0
        u["cost"] = round(u["cost"] + c, 4)
        u["days"][day] = round(u["days"].get(day, 0) + c, 4)
        if s.get("round"):
            s["round"]["cost"] = round(s["round"].get("cost", 0) + c, 4)
        if sub is not None:
            sub["cost"] = round(sub.get("cost", 0) + c, 4)
            sub["tokens"] = sub.get("tokens", 0) + sum(x.get(k) or 0 for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        elif model and not model.startswith("<"):  # "<synthetic>" messages aren't real model calls
            u["ctx"] = sum(x.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"))
            u["ctx_max"], u["model"] = window(model), model


def lan_token():
    tok = read_json(os.path.join(DIR, "lan.json"), {})
    return tok.get("token") if isinstance(tok, dict) else None


def lan_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))  # picks the Wi-Fi interface; sends nothing
            return s.getsockname()[0]
        except OSError:
            return None


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


@contextlib.contextmanager
def locked():
    """(progress, board) loaded under the lock; each is written back only if it changed."""
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # several sessions and the server write at once
        d, b = read_json(DATA, {}), read_json(BOARD, {})
        d = d if isinstance(d, dict) else {}
        b = b if isinstance(b, dict) else {}
        d.setdefault("sessions", {})
        for k, v in (("proposals", {}), ("requests", []), ("listeners", {}), ("seq", 0), ("settings", {}), ("notes", {}), ("perms", {})):
            b.setdefault(k, v)
        before = json.dumps(d, sort_keys=True), json.dumps(b, sort_keys=True)
        yield d, b
        if json.dumps(d, sort_keys=True) != before[0]:
            write_json(DATA, d)
        if json.dumps(b, sort_keys=True) != before[1]:
            write_json(BOARD, b)


# ---------------------------------------------------------------- server

def ping():
    """What the server on our port reports: (version, phone access on), or None if it isn't ours."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/ping", timeout=0.5) as r:
            j = json.loads(r.read())
            return j.get("v"), bool(j.get("lan"))
    except Exception:
        return None


def port_open(port=None):
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port or PORT)) == 0


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
        root = os.path.abspath(ROOT)
        if add:
            ps[root] = {"name": os.path.basename(root), "port": PORT, "added": (ps.get(root) or {}).get("added") or now()}
        else:
            ps.pop(root, None)
        for gone in [k for k in ps if not os.path.isdir(os.path.join(k, ".progress"))]:
            ps.pop(gone)
        if json.dumps(reg, sort_keys=True) != before:
            write_json(REG, reg)


def fresh(name, secs):
    try:
        return time.time() - os.path.getmtime(os.path.join(HUB_DIR, name)) < secs
    except OSError:
        return False


def touch(path):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
    except OSError:
        pass


def write_launcher():
    """~/.claude/project-plan/hook.py runs whichever copy is installed, so commands in rules and docs never change path."""
    me = os.path.realpath(__file__)
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


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v or ""))[:3])


def update_info():
    """{"current", "latest"} when a newer plugin version is on GitHub (checked at most daily, in the background), else None."""
    if not IS_PLUGIN or not APP_VERSION:
        return None
    u = read_json(os.path.join(HUB_DIR, "update.json"), {})
    if not isinstance(u, dict) or age(u.get("checked")) > 86400:
        subprocess.Popen([sys.executable, os.path.realpath(__file__), "check-update"], cwd=HUB_DIR if os.path.isdir(HUB_DIR) else None,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    latest = (u or {}).get("latest") if isinstance(u, dict) else None
    return {"current": APP_VERSION, "latest": latest} if latest and vtuple(latest) > vtuple(APP_VERSION) else None


def check_update():
    """Ask GitHub (through the gh CLI, so private-repo access uses the user's login) for the published plugin version."""
    out = {"checked": now()}
    try:
        r = subprocess.run(["gh", "api", f"repos/{REPO}/contents/plugins/project-plan/.claude-plugin/plugin.json",
                            "-H", "Accept: application/vnd.github.raw"], capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            out["latest"] = json.loads(r.stdout).get("version")
        else:
            out["error"] = short(r.stderr, 200)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        out["error"] = short(str(e), 200)
    os.makedirs(HUB_DIR, exist_ok=True)
    write_json(os.path.join(HUB_DIR, "update.json"), out)
    return out


def update_text(u):
    return (f"project-plan {u['latest']} is available (this machine has {u['current']}). Update with "
            "`claude plugin marketplace update` then `claude plugin update project-plan`, and restart Claude Code.")


def plugin_enabled():
    """The plugin is installed and enabled: then a hand install's project hooks step aside, so nothing runs twice."""
    try:
        return any(k.startswith("project-plan@") and v for k, v in (read_json(os.path.expanduser("~/.claude/settings.json"), {}).get("enabledPlugins") or {}).items())
    except AttributeError:
        return False


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
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "hub"], cwd=HUB_DIR,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def ensure_server():
    """Keep the page in step with the skill's copy, and our server (current version) on the port."""
    for page in ("index.html", "map.html"):
        src, dst = os.path.join(SKILL, page), os.path.join(DIR, page)
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
    if port_open() and not stop_ours(PORT, ["http.server", "hook.py serve"]):
        return  # something else owns the port: leave it alone
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve"], cwd=ROOT,
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
    st = read_json(BOARD, {}).get("settings") or {}
    if not st.get("ntfy_topic"):
        raise ValueError("Save a topic first.")
    try:
        send_push(st, "Project board", f"Test alert from the {os.path.basename(ROOT)} board. Alerts arrive here when you're away from the board.")
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
    git = lambda *a: subprocess.run(["git", "-C", ROOT, *a], capture_output=True, text=True, timeout=5)
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
    root = os.path.realpath(ROOT)
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
        r = subprocess.run([TE_BIN, "--host", "claude", "--cwd", ROOT, *a], capture_output=True, text=True, timeout=timeout)
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


def api_map(path, body):
    """The Map view: rescan, dismiss a health issue, notes, Ask, flows on request, and blueprint undo."""
    import codemap, mapapp
    codemap.setup(ROOT, DIR)
    if path == "/api/map/rescan":
        with MAP_LOCK:
            mapapp.planned(quiet=True) if mapapp.blueprint_mode() else codemap.build(quiet=True)
        return 200, {"ok": True}
    if path == "/api/map/dismiss":
        f = os.path.join(DIR, "map", "dismissed.json")
        with MAP_LOCK:
            d = read_json(f, {})
            if body.get("on", True):
                d[clip(body.get("id"), 200)] = {"at": now(), "reason": clip(body.get("reason"), 500)}
            else:
                d.pop(str(body.get("id")), None)
            write_json(f, d)
            mapapp.planned(quiet=True) if mapapp.blueprint_mode() else codemap.build(quiet=True)
        return 200, {"ok": True}
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


def board_seen():
    """A board tab (or the all-projects hub) was open and visible in the last few seconds."""
    try:
        if time.time() - os.path.getmtime(os.path.join(DIR, ".seen")) < 12:
            return True
    except OSError:
        pass
    return fresh(".hub-seen", 12)


def wait_permit(sid, pid, until):
    """Poll for the user's answer on the board until the deadline: allow, deny or None (let the terminal ask)."""
    while time.time() < until:
        time.sleep(0.5)
        v = read_json(BOARD, {}).get("perms", {}).get(pid)
        if v:
            return v.get("decision")
        n = read_json(DATA, {}).get("sessions", {}).get(sid, {}).get("needs") or {}
        if n.get("id") != pid:  # answered: the board writes progress.json just before board.json
            time.sleep(0.2)
            return (read_json(BOARD, {}).get("perms", {}).get(pid) or {}).get("decision")
    return None


def budget_notes(d, b, s):
    """Warn Claude once per day per limit when today's spend or this session's context passes the user's limits."""
    st, out, day = b.get("settings") or {}, [], datetime.now().strftime("%Y-%m-%d")
    w = s.get("warned") if (s.get("warned") or {}).get("day") == day else {"day": day}
    spent = sum((x.get("usage") or {}).get("days", {}).get(day, 0) for x in d["sessions"].values())
    if st.get("budget_day") and spent >= st["budget_day"] and not w.get("spend"):
        w["spend"] = True
        out.append(f"Budget: today's spend in this project is ${spent:.2f} at API rates, past the ${st['budget_day']:.2f} daily budget the user set on the board. "
                   "Finish the current task without starting new work, keep the rest of the work tight, and tell the user in one line that the budget is used up.")
    u = s.get("usage") or {}
    pct = round(u["ctx"] / u["ctx_max"] * 100) if u.get("ctx") and u.get("ctx_max") else 0
    if st.get("ctx_limit") and pct >= st["ctx_limit"] and not w.get("ctx"):
        w["ctx"] = True
        out.append(f"Context: this session has used {pct}% of its context window, past the {st['ctx_limit']}% limit the user set on the board. "
                   "Wrap up the current task, then suggest a fresh session for the next one.")
    s["warned"] = w
    return out


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
                codemap.setup(ROOT, DIR)
                o = parse_qs(url.query)
                on = lambda k, d: o.get(k, [d])[0] == "1"
                hist = open(HIST).read() if on("recent", "0") and os.path.exists(HIST) else ""
                try:
                    data = mapapp.export(on("flows", "1"), on("notes", "1"), on("recent", "0"), on("health", "0"), hist).encode()
                except (OSError, ValueError) as e:
                    return self.send(400, {"error": str(e)})
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(ROOT)}-map.html"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            if url.path == "/api/diff":
                q = parse_qs(url.query)
                return self.send(*api_diff(q.get("path", [""])[0], q.get("full", [""])[0] == "1"))
            if url.path == "/progress.json" and "seen" in url.query:  # a visible board tab: permission requests wait for it
                try:
                    open(os.path.join(DIR, ".seen"), "w").close()
                except OSError:
                    pass
            if url.path == "/api/lan":
                ip = lan_ip() if token else None
                return self.send(200, {"on": bool(token), "url": f"http://{ip}:{PORT}/?key={token}" if self.local() and ip else ""})
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

    ThreadingHTTPServer(("0.0.0.0" if token else "127.0.0.1", PORT), functools.partial(Handler, directory=DIR)).serve_forever()


# ---------------------------------------------------------------- session tracking

def last_reply(ev):
    """Claude's last text reply: from the Stop event if given, else the transcript tail."""
    if ev.get("last_assistant_message"):
        return short(ev["last_assistant_message"], 500)
    path = ev.get("transcript_path")
    if not path or not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        f.seek(max(0, os.path.getsize(path) - 400_000))
        lines = f.read().decode("utf-8", "ignore").splitlines()
    for line in reversed(lines):
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if j.get("type") == "assistant":
            content = (j.get("message") or {}).get("content") or []
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            if any(t.strip() for t in texts):
                return short(" ".join(texts), 500)
    return ""


def split_title(text):
    for sep in (" — ", " -- ", " – "):
        if sep in text:
            title, _, summary = text.partition(sep)
            return title.strip(), summary.strip()
    return text.strip(), ""


def status_of(x):
    return {"completed": "done"}.get(x, x or "pending")


def stamp(task, t):
    if task["status"] == "in_progress" and not task.get("started"):
        task["started"] = t
    if task["status"] == "done" and not task.get("finished"):
        task["finished"] = t


EDIT_TOOLS = ("Edit", "MultiEdit", "NotebookEdit", "Write")
STAT = {"Bash": "commands", "Read": "reads", "Grep": "searches", "Glob": "searches", "Agent": "agents", "Task": "agents"}
# Only a command that starts a shell segment counts, so "pnpm test" quoted inside another command doesn't.
CHECK_RE = re.compile(r"(?:^|&&|\|\||;|\n)\s*(?:npx\s+)?((?:pnpm|npm|yarn|bun)\s+(?:run\s+)?(?:check|test|lint|typecheck|build|e2e)\b|vitest\b|tsc\b|eslint\b|playwright test\b)")
FAIL_RE = re.compile(r"\b([1-9]\d*) (?:failed|errors?|problems?)\b|error TS\d+|ELIFECYCLE|Command failed", re.I)
RESULT_RE = re.compile(r"^\s*(?:Tests?|Test Files)\s+.*(?:passed|failed).*$|^.*\b\d+ (?:failed|passed|problems?|errors?)\b.*$", re.M)


def label(tool, inp):
    """One readable line for a tool call, and its kind (drives the icon)."""
    path = rel(inp.get("file_path") or inp.get("notebook_path"))
    if tool in EDIT_TOOLS:
        return ("Wrote " if tool == "Write" else "Edited ") + str(path), "edit"
    if tool == "Bash":
        return "Ran: " + short(inp.get("description") or inp.get("command")), "run"
    if tool == "Read":
        return f"Read {path}", "read"
    if tool in ("Grep", "Glob"):
        return f"Searched for {short(inp.get('pattern'), 60)}", "search"
    if tool in ("Agent", "Task"):
        return "Started agent: " + short(inp.get("description")), "agent"
    if tool in ("WebFetch", "WebSearch"):
        return f"{tool}: " + short(inp.get("url") or inp.get("query")), "web"
    return tool.replace("mcp__", "").replace("__", " · "), "tool"


def lines(text):
    return len(str(text or "").splitlines())


def line_counts(tool, inp, res):
    """(+added, −removed) for an edit, from the tool's patch when it gives one."""
    patch = res.get("structuredPatch") if isinstance(res, dict) else None
    if patch:
        hunk_lines = [l for h in patch for l in h.get("lines", [])]
        return sum(l.startswith("+") for l in hunk_lines), sum(l.startswith("-") for l in hunk_lines)
    if tool == "Write":
        return lines(inp.get("content")), 0
    edits = inp.get("edits") or [inp]
    return sum(lines(e.get("new_string")) for e in edits), sum(lines(e.get("old_string")) for e in edits)


def edit_hunks(tool, inp, res):
    """The edit as unified-diff hunks [{old_start, new_start, lines: [" x", "-y", "+z"]}], for the before/after view."""
    patch = res.get("structuredPatch") if isinstance(res, dict) else None
    if patch:
        return [{"old_start": h.get("oldStart", 0), "new_start": h.get("newStart", 0), "lines": h.get("lines", [])} for h in patch]
    if tool == "Write":  # a new file: everything is added
        return [{"old_start": 0, "new_start": 1, "lines": ["+" + l for l in str(inp.get("content") or "").split("\n")]}]
    hunks = []
    for e in inp.get("edits") or [inp]:
        old, new = str(e.get("old_string") or ""), str(e.get("new_string") or "")
        if old or new:
            hunks.append({"old_start": 0, "new_start": 0, "lines": ["-" + l for l in old.split("\n") if old] + ["+" + l for l in new.split("\n") if new]})
    return hunks


def save_edit(rid, path, tool, inp, res, t, vs_head=False):
    """Append one edit to .progress/edits/<round>.jsonl, so History can show what each request changed."""
    hunks = edit_hunks(tool, inp, res)
    if not hunks:
        return
    line = json.dumps({"path": path, "tool": tool, "at": t, "hunks": hunks, **({"vs_head": True} if vs_head else {})})
    if len(line) > 400_000:  # ponytail: huge edits (a generated file) are skipped; the git diff still shows them
        line = json.dumps({"path": path, "tool": tool, "at": t, "hunks": [], "cut": True})
    try:
        os.makedirs(os.path.join(DIR, "edits"), exist_ok=True)
        with open(os.path.join(DIR, "edits", rid.replace(":", "-") + ".jsonl"), "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def note_file(s, path, add, rem, t):
    """Add an edit to the session's changed-files list and the current request's."""
    files = [f if isinstance(f, dict) else {"path": f, "add": 0, "del": 0} for f in s.get("files", [])]
    old = next((f for f in files if f["path"] == path), {"add": 0, "del": 0})
    files = [f for f in files if f["path"] != path] + [{"path": path, "add": old["add"] + add, "del": old["del"] + rem, "at": t}]
    s["files"] = files[-MAX_FILES:]  # most recent last
    r = s.get("round")
    if r:
        rf = r.setdefault("files", {}).setdefault(path, [0, 0])
        rf[0], rf[1] = rf[0] + add, rf[1] + rem


def git_snap():
    """{path: [mtime, +lines, -lines]} for every uncommitted file in the project, or None outside git."""
    git = lambda *a: subprocess.run(["git", "-C", ROOT, *a], capture_output=True, text=True, timeout=5)
    try:
        prefix = git("rev-parse", "--show-prefix")
        if prefix.returncode:
            return None
        prefix = prefix.stdout.strip()
        status = git("status", "--porcelain=v1", "-uall", "--", ".").stdout
        numstat = git("diff", "--numstat", "HEAD", "--", ".").stdout
    except (OSError, subprocess.SubprocessError):
        return None
    cut = lambda p: p[len(prefix):] if p.startswith(prefix) else p
    ns = {}
    for line in numstat.splitlines():
        a, d, p = (line.split("\t", 2) + ["", ""])[:3]
        ns[cut(p)] = [int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0]
    snap = {}
    for line in status.splitlines():
        p = cut(line[3:].split(" -> ")[-1].strip('"'))
        full = os.path.join(ROOT, p)
        try:
            m = os.path.getmtime(full)
        except OSError:
            m = 0  # deleted
        if line.startswith("??"):
            try:
                n = sum(1 for _ in open(full, "rb")) if os.path.getsize(full) < 2_000_000 else 0
            except OSError:
                n = 0
            ns[p] = [n, 0]
        snap[p] = [m, *ns.get(p, [0, 0])]
    return snap


def shell_edits(s, t):
    """Files a shell command changed (sed -i, a heredoc, a script): what moved since the last snapshot.

    ponytail: mtime-based, so a file you save in your editor while Claude runs a command is credited to
    that command; a per-command PreToolUse snapshot would fix it but needs the hook matcher widened."""
    before, after = s.get("gitsnap"), git_snap()
    s["gitsnap"] = after
    if before is None or after is None:
        return
    r = s.get("round")
    for p, (m, a, d) in after.items():
        b = before.get(p)
        if b and b[0] == m or p.startswith(".progress/"):
            continue
        add, rem = max(0, a - (b[1] if b else 0)), max(0, d - (b[2] if b else 0))
        note_file(s, p, add, rem, t)
        if r:
            save_shell_edit(r["id"], p, t)


def save_shell_edit(rid, path, t):
    """The file's diff against the last commit, right after the shell command that changed it."""
    try:
        out = subprocess.run(["git", "-C", ROOT, "diff", "--no-color", "--no-ext-diff", "HEAD", "--", path],
                             capture_output=True, text=True, timeout=5).stdout
        if not out and os.path.isfile(os.path.join(ROOT, path)):  # untracked: the whole file is new
            out = subprocess.run(["git", "-C", ROOT, "diff", "--no-color", "--no-index", "--", "/dev/null", path],
                                 capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return
    hunks, h = [], None
    for line in out.splitlines():
        mm = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if mm:
            h = {"old_start": int(mm.group(1)), "new_start": int(mm.group(2)), "lines": []}
            hunks.append(h)
        elif h is not None and line[:1] in (" ", "+", "-"):
            h["lines"].append(line)
    if hunks:
        save_edit(rid, path, "Bash", {}, {"structuredPatch": [{"oldStart": x["old_start"], "newStart": x["new_start"], "lines": x["lines"]} for x in hunks]}, t, vs_head=True)


def track(s, tool, inp, res, t):
    """Counters, the changed-files list and the latest check result."""
    st, r = s.setdefault("stats", {}), s.get("round")
    st["tools"] = st.get("tools", 0) + 1
    if r:
        r["tools"] = r.get("tools", 0) + 1
    if tool in EDIT_TOOLS:
        st["edits"] = st.get("edits", 0) + 1
        path = rel(inp.get("file_path") or inp.get("notebook_path"))
        add, rem = line_counts(tool, inp, res)
        note_file(s, path, add, rem, t)
        if r:
            save_edit(r["id"], path, tool, inp, res, t)
        if s.get("gitsnap") is not None:
            s["gitsnap"] = git_snap()  # so the next shell command isn't credited with this edit
    elif tool in STAT:
        st[STAT[tool]] = st.get(STAT[tool], 0) + 1
    if tool == "Bash":
        shell_edits(s, t)
        m = CHECK_RE.search(inp.get("command", ""))
        if m:
            out = (res.get("stdout", "") + "\n" + res.get("stderr", "")) if isinstance(res, dict) else str(res)
            record_check(s, m.group(1), not FAIL_RE.search(out), out, t)


def record_check(s, name, ok, out, t):
    found, tail = RESULT_RE.findall(out or ""), (out or "").strip().splitlines()
    s["check"] = {"name": name, "ok": ok, "at": t, "detail": short(found[-1] if found else tail[-1] if tail else "", 120)}
    # Check history: "edit" is the newest file edit at the time, so a fail then a pass with no edit between is flaky.
    edit = max((f.get("at", "") for f in s.get("files", []) if isinstance(f, dict)), default="")
    s["runs"] = (s.get("runs", []) + [{"name": name, "ok": ok, "at": t, "edit": edit}])[-30:]


def auto_title(text):
    """A short session title from a prompt: no tags, quote marks or markdown, the first sentence if it's long enough."""
    t = re.sub(r"<[^>]+>", " ", str(text or ""))
    t = re.sub(r"[`*_#]", "", t)
    t = " ".join(re.sub(r"^[\s>❯›»\-]+", "", t).split())
    m = re.match(r"(.{16,}?[.!?])(\s|$)", t)
    return short(m.group(1) if m else t, 70)


def claude_pid():
    """The Claude Code process this hook runs under (hook → shell → claude), so a session whose process died can be ended."""
    pid = os.getppid()
    for _ in range(4):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True, timeout=1).stdout.split(None, 1)
        except (OSError, subprocess.SubprocessError):
            return None
        if len(out) < 2:
            return None
        if os.path.basename(out[1].strip()).startswith("claude"):
            return pid
        pid = int(out[0])
    return None


def sweep(d, b, t):
    """End sessions whose Claude process is gone, and (once a day) drop history older than the user's keep-days setting."""
    for s in d["sessions"].values():
        if s.get("status") != "ended" and s.get("pid") and not alive(s["pid"]):
            s["status"], s["updated"] = "ended", t
            s.pop("needs", None)
            archive(s)
            log(s, t, "Claude exited without ending the session", "session")
    keep, day = (b.get("settings") or {}).get("keep_days"), datetime.now().strftime("%Y-%m-%d")
    if keep and b.get("pruned") != day and os.path.exists(HIST):
        b["pruned"] = day
        cut = datetime.fromtimestamp(time.time() - keep * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [l for l in open(HIST) if (lambda e: (e.get("ended") or e.get("started") or "") >= cut)(json.loads(l) if l.strip().startswith("{") else {})]
        with open(HIST + ".tmp", "w") as f:
            f.writelines(lines)
        os.replace(HIST + ".tmp", HIST)
        pv = os.path.join(DIR, "previews")
        for name in os.listdir(pv) if os.path.isdir(pv) else []:
            if os.path.getmtime(os.path.join(pv, name)) < time.time() - keep * 86400:
                os.remove(os.path.join(pv, name))


def push(title, message):
    """Phone alert through ntfy, sent from a detached process so the hook never waits on the network."""
    st = read_json(BOARD, {}).get("settings") or {}
    if st.get("ntfy_topic") and not board_seen():  # only when the user isn't looking at the board
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "push", short(title, 120), short(message, 400)], cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def send_push(st, title, message):
    import urllib.request
    body = json.dumps({"topic": st["ntfy_topic"], "title": title, "message": message or title, "tags": ["robot"],
                       "click": f"http://localhost:{PORT}"}).encode()
    req = urllib.request.Request(st.get("ntfy_server") or "https://ntfy.sh", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.status


OPEN = ("pending", "in_progress", "blocked")


def drift(s, was_open):
    """Remember the plan as first written; tasks added once work has started are marked "added later"."""
    tasks = s["tasks"]
    if not was_open and any(x["status"] in OPEN for x in tasks):  # a fresh plan starts
        s["plan0"], s["frozen"] = [], False
        for x in tasks:
            x.pop("added", None)
    plan0 = s.setdefault("plan0", [])
    for x in tasks:
        if x["title"] not in plan0 and not x.get("added"):
            if s.get("frozen"):
                x["added"] = True
            else:
                plan0.append(x["title"])
    if any(x["status"] in ("in_progress", "done") for x in tasks if x["title"] in plan0):
        s["frozen"] = True


def agent_for(s, ev, t):
    """The subagent entry a hook event belongs to (by agent_id), created if the start was missed."""
    agents = s.setdefault("agents", [])
    a = next((a for a in agents if a.get("agent_id") == ev.get("agent_id")), None)
    if not a:
        a = {"id": ev.get("agent_id"), "agent_id": ev.get("agent_id"), "type": ev.get("agent_type") or "agent",
             "desc": ev.get("agent_type") or "Subagent", "status": "running", "started": t, "tools": 0}
        agents.append(a)
        s["agents"] = agents[-30:]
    return a


def session(d, sid, t):
    return d["sessions"].setdefault(sid, {"id": sid, "title": "", "started": t, "status": "active", "tasks": [], "log": []})


def log(s, t, text, kind="info"):
    s.setdefault("log", []).append({"at": t, "text": text, "kind": kind})
    s["log"] = s["log"][-MAX_LOG:]


def archive(s):
    """Append the current round (one request and what was done for it) to history.jsonl.

    Rewritten whenever it changes; the page keeps the last line per id."""
    r = s.get("round")
    if not r:
        return
    t0 = r["started"]
    entry = {
        "id": r["id"], "session": s["id"], "title": s.get("name") or s.get("title", ""), "project": s.get("project", ""),
        "prompt": r.get("prompt", ""), "source": r.get("source", "terminal"), "started": t0, "ended": s.get("updated", t0),
        "done": [{k: x.get(k, "") for k in ("title", "summary", "started", "finished", "added")}
                 for x in s.get("tasks", []) if x["status"] == "done" and (x.get("finished") or "") >= t0],
        "open": [x["title"] for x in s.get("tasks", []) if x["status"] in OPEN],
        "planned": s.get("plan0", []),
        "dropped": [p for p in s.get("plan0", []) if p not in {x["title"] for x in s.get("tasks", [])}],
        "files": [{"path": p, "add": a, "del": rm} for p, (a, rm) in r.get("files", {}).items()],
        "tools": r.get("tools", 0), "errors": r.get("errors", 0), "proposals": r.get("proposals", []),
        "cost": r.get("cost", 0),
        "agents": [{k: a.get(k) for k in ("desc", "type", "status", "started", "finished")}
                   for a in s.get("agents", []) if (a.get("started") or "") >= t0],
        # the whole task list as it stood (History shows it even when nothing finished in this round)
        "tasks": [{k: x.get(k, "") for k in ("title", "summary", "status", "started", "finished", "added")} for x in s.get("tasks", [])],
        "log": [x for x in s.get("log", []) if (x.get("at") or "") >= t0][-150:],
    }
    if s.get("check") and s["check"].get("at", "") >= t0:
        entry["check"] = s["check"]
    if (s.get("reply_at") or "") >= t0 and s.get("reply"):
        entry["reply"] = s["reply"]
    sig = hashlib.md5(json.dumps({k: v for k, v in entry.items() if k not in ("ended", "log")}, sort_keys=True).encode()).hexdigest()
    if r.get("sig") == sig:
        return
    r["sig"] = sig
    # ponytail: history.jsonl grows forever (a few hundred bytes per request); trim it if it ever gets big
    with open(HIST, "a") as f:
        f.write(json.dumps(entry) + "\n")


def new_round(s, prompt, t, source="terminal"):
    archive(s)
    s["rounds"] = s.get("rounds", 0) + 1
    s["round"] = {"id": f"{s['id']}:{s['rounds']}", "prompt": short(prompt, 400), "source": source, "started": t,
                  "files": {}, "tools": 0, "errors": 0, "proposals": []}


def apply(d, ev):
    sid, name, t = ev.get("session_id", "unknown"), ev.get("hook_event_name"), now()
    s = session(d, sid, t)
    s["updated"] = d["updated"] = t
    s["project"] = os.path.basename(str(ROOT).rstrip("/"))  # the board's project, even if the shell cd'd elsewhere
    say = lambda text, kind="info": log(s, t, text, kind)
    tool, inp = ev.get("tool_name", ""), ev.get("tool_input") or {}
    if name in ("PostToolUse", "PostToolUseFailure", "UserPromptSubmit", "Stop", "SessionEnd"):
        s.pop("needs", None)  # whatever it was waiting on has been answered
    was_open = any(x["status"] in OPEN for x in s["tasks"])
    sub = ev.get("agent_id") if name not in ("SubagentStart", "SubagentStop") else None  # event from inside a subagent

    # Tokens, cost and context fill: read what the main transcript gained since last time.
    tp = ev.get("transcript_path")
    if tp and not sub and name in ("PostToolUse", "PostToolUseFailure", "UserPromptSubmit", "Stop", "SessionEnd", "SubagentStop"):
        if s.get("tx_path") != tp:
            s["tx_path"], s["tx_off"], s["tx_last"] = tp, 0, None
        s["tx_off"], s["tx_last"], msgs = read_usage(tp, s["tx_off"], s["tx_last"])
        add_usage(s, msgs)

    if sub and name in ("PostToolUse", "PostToolUseFailure"):
        # A subagent's tool call: count it under that agent, keep its edits in the files list, keep it out of the main feed.
        a = agent_for(s, ev, t)
        a["tools"], a["last"], a["at"] = a.get("tools", 0) + 1, label(tool, inp)[0], t
        if name == "PostToolUse":
            track(s, tool, inp, ev.get("tool_response"), t)
        else:
            a["errors"] = a.get("errors", 0) + 1
    elif name == "PreToolUse":
        if tool in ("Agent", "Task"):
            s["status"] = "working"
            s.setdefault("agents", []).append({
                "id": ev.get("tool_use_id", ""), "desc": short(inp.get("description") or inp.get("subagent_type"), 80),
                "type": inp.get("subagent_type") or "general-purpose", "status": "running", "started": t,
                "bg": bool(inp.get("run_in_background")), "parent": ev.get("agent_id"), "tools": 0})
            s["agents"] = s["agents"][-30:]
            say("Started agent: " + short(inp.get("description")), "agent")
    elif name == "SubagentStart":
        agents = s.setdefault("agents", [])
        waiting = [a for a in agents if a["status"] == "running" and not a.get("agent_id")]
        a = next((a for a in waiting if a.get("type") == ev.get("agent_type")), waiting[0] if waiting else None) or agent_for(s, ev, t)
        a["agent_id"] = ev.get("agent_id")
    elif name == "SubagentStop":
        a = agent_for(s, ev, t)
        a.update(status="done", finished=t, reply=short(ev.get("last_assistant_message"), 300))
        if ev.get("agent_transcript_path"):
            add_usage(s, read_usage(ev["agent_transcript_path"])[2], sub=a)
        say("Agent finished: " + short(a.get("desc")), "done")
    elif name == "PermissionRequest":
        what = label(tool, inp)[0]
        if tool == "Bash" and inp.get("command") and short(inp["command"], 60) not in what:
            what += f" ({short(inp['command'], 120)})"
        s["needs"] = {"at": t, "what": what, "id": secrets.token_hex(6), "tool": tool}
        if board_seen():
            s["needs"]["until"] = datetime.fromtimestamp(time.time() + PERMIT_WAIT, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        say("Waiting for your approval: " + what, "needs")
    elif name == "Notification":
        if ev.get("notification_type") == "permission_prompt" and "needs" not in s:
            s["needs"] = {"at": t, "what": short(ev.get("message"), 140)}
            say("Waiting for your approval", "needs")
    elif name == "PostToolUseFailure":
        s["status"] = "working"
        s["errors"] = s.get("errors", 0) + 1
        if s.get("round"):
            s["round"]["errors"] = s["round"].get("errors", 0) + 1
        err = short(ev.get("error") or "failed", 160)
        say(f"Failed: {label(tool, inp)[0]} · {err}", "error")
        a = next((a for a in s.get("agents", []) if a.get("id") == ev.get("tool_use_id")), None) if tool in ("Agent", "Task") else None
        if a:
            a.update(status="failed", finished=t)
        m = CHECK_RE.search(inp.get("command", "")) if tool == "Bash" else None
        if m and not ev.get("is_interrupt"):
            record_check(s, m.group(1), False, str(ev.get("error") or ""), t)
    elif name == "SessionStart":
        s["status"] = "active"
        s["pid"] = claude_pid() or s.get("pid")
        if ev.get("model"):
            s["model"] = ev["model"] if isinstance(ev["model"], str) else ev["model"].get("id", "")
        say(f"Session {ev.get('source', 'started')}", "session")
    elif name == "UserPromptSubmit" and str(ev.get("prompt", "")).lstrip().startswith("<task-notification>"):
        # a background command finished (e.g. the board listener): not a new request from the user
        s["status"] = "working"
        m = re.search(r"<summary>(.*?)</summary>", ev["prompt"], re.S)
        say("Background task: " + short(m.group(1) if m else "finished", 160), "info")
    elif name == "UserPromptSubmit":
        s["status"] = "working"
        s["title"] = s["title"] or auto_title(ev.get("prompt"))
        s["pid"] = claude_pid() or s.get("pid")
        s["prompts"] = s.get("prompts", 0) + 1
        new_round(s, ev.get("prompt"), t)
        s["gitsnap"] = git_snap()  # baseline for files shell commands change in this request
        say("You: " + short(ev.get("prompt"), 160), "you")
    elif name == "PostToolUse":
        s["status"] = "working"
        if tool == "TodoWrite":
            old = {x["title"]: x for x in s["tasks"]}
            tasks = []
            for x in inp.get("todos", []):
                title, summary = split_title(x.get("content", ""))
                task = {**{k: v for k, v in old.get(title, {}).items() if k in ("started", "finished", "added")},
                        "title": title, "summary": summary, "status": status_of(x.get("status")), "active": x.get("activeForm", "")}
                stamp(task, t)
                tasks.append(task)
            s["tasks"] = tasks
            say("Updated the plan", "plan")
        elif tool == "TaskCreate":
            res = ev.get("tool_response")
            res = res if isinstance(res, dict) else {}
            tid = str((res.get("task") or {}).get("id") or res.get("id") or len(s["tasks"]) + 1)
            s["tasks"].append({"id": tid, "title": inp.get("subject", ""), "summary": short(inp.get("description"), 200),
                               "status": "pending", "active": inp.get("activeForm", "")})
            say("Planned: " + short(inp.get("subject")), "plan")
        elif tool == "TaskUpdate":
            for x in list(s["tasks"]):
                if x.get("id") != str(inp.get("taskId")):
                    continue
                if inp.get("status") == "deleted":
                    s["tasks"].remove(x)
                    say("Dropped task: " + short(x["title"]), "plan")
                    continue
                before = x["status"]
                x["status"] = status_of(inp.get("status") or x["status"])
                x["title"] = inp.get("subject") or x["title"]
                x["summary"] = short(inp.get("description"), 200) if inp.get("description") else x.get("summary", "")
                x["active"] = inp.get("activeForm") or x.get("active", "")
                stamp(x, t)
                if x["status"] != before:
                    say(("Started: " if x["status"] == "in_progress" else "Done: " if x["status"] == "done" else x["status"] + ": ") + short(x["title"]),
                        "done" if x["status"] == "done" else "plan")
        elif tool in ("Agent", "Task"):
            track(s, tool, inp, ev.get("tool_response"), t)
            a = next((a for a in s.get("agents", []) if a.get("id") == ev.get("tool_use_id")), None)
            if a and not a.get("bg") and a["status"] == "running":  # a foreground agent returned (SubagentStop may also say so)
                a.update(status="done", finished=t)
        else:
            track(s, tool, inp, ev.get("tool_response"), t)
            say(*label(tool, inp))
    elif name == "CLI":
        cmd, *rest = ev["args"]
        if cmd in ("plan", "add"):
            new = [dict(zip(("title", "summary"), split_title(x)), status="pending") for x in rest]
            s["tasks"] = new if cmd == "plan" else s["tasks"] + new
            say("Planned " + (f"{len(new)} tasks" if cmd == "plan" else "task: " + new[0]["title"]), "plan")
        elif cmd == "task":
            x = s["tasks"][int(rest[0]) - 1]
            x["status"] = status_of(rest[1])
            stamp(x, t)
            say(("Started: " if x["status"] == "in_progress" else "Done: " if x["status"] == "done" else x["status"] + ": ") + short(x["title"]),
                "done" if x["status"] == "done" else "plan")
    elif name == "Stop":
        s["status"] = "idle"
        s["reply"], s["reply_at"] = last_reply(ev), t
        archive(s)
        say("Finished responding, waiting for you", "stop")
    elif name == "SessionEnd":
        s["status"] = "ended"
        archive(s)
        say("Session ended", "session")

    if name in ("PostToolUse", "CLI") and not sub:
        drift(s, was_open)
    # ponytail: keeps only the newest sessions live; their requests stay in history.jsonl
    keep = sorted(d["sessions"].values(), key=lambda x: x.get("updated", ""), reverse=True)[:MAX_SESSIONS]
    d["sessions"] = {x["id"]: x for x in keep}
    return s


# ---------------------------------------------------------------- proposals and board requests

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
        os.makedirs(os.path.join(DIR, "previews"), exist_ok=True)
        v["preview"] = f"previews/{p['id']}-v{n}.html"
        with open(os.path.join(DIR, v["preview"]), "w") as f:
            f.write(spec["preview"])
    p["versions"].append(v)
    p.update(status="awaiting", updated=t, session_title=s.get("title", ""))
    p.pop("delivered", None)
    if s.get("round") and p["id"] not in s["round"]["proposals"]:
        s["round"]["proposals"].append(p["id"])
    log(s, t, f"{'Revised' if revise else 'Proposed'} on the board: {short(p['title'], 80)}", "plan")
    return p["id"]


ANSWERED = ("approved", "revise", "dismissed")


def reply_text(p):
    v = p["versions"][-1]
    res, pid = v.get("response") or {}, p["id"]
    dec = res.get("decision", "approve")
    out = [f"Board: the user {dict(approve='APPROVED', revise='ASKED FOR CHANGES TO', dismiss='DISMISSED')[dec]} proposal {pid} “{p['title']}” (v{v['n']})."]
    qs = {q["id"]: q for q in v.get("questions", [])}
    answers = [(qs[k]["text"], ", ".join(a) if isinstance(a, list) else a) for k, a in (res.get("answers") or {}).items() if k in qs and a]
    if answers:
        out += ["Their answers:"] + [f"- {q} → {a}" for q, a in answers]
    if res.get("instructions"):
        out.append("Additional instructions: " + res["instructions"])
    out.append("Next: " + {
        "approve": f"build it now. Run `{H} proposal {pid} building`, write the plan with your task list tool following their answers and "
                   f"instructions, do the work, run the checks, then `{H} proposal {pid} done`.",
        "revise": f"don't build yet. Rework the proposal with this feedback and post it again with `{H} propose <file|-> --revise {pid}`.",
        "dismiss": "don't build it. Acknowledge it in one line.",
    }[dec])
    return "\n".join(out)


def request_text(r):
    what = {
        "brainstorm": "wants to brainstorm this with you. Explore the codebase as needed, then post a proposal on the board "
                      f"(`{H} propose`) laying out the idea, the options and the questions only they can answer. Don't build until they approve it.",
        "suggest": "asks you to suggest features. Look at the project, then post a proposal on the board "
                   f"(`{H} propose`) with 3–6 concrete feature ideas as a \"many\" question so they can pick, each with a one-line detail "
                   "and the effort it takes. Don't build until they approve.",
        "task": "asks you to do this task. Plan it with your task list tool and do it. If it's unclear or has real "
                f"alternatives, post a proposal (`{H} propose`) first and wait for their answer.",
    }[r["kind"]]
    out = [f"Board: the user {what}", "Request: " + r["text"]]
    if r.get("instructions"):
        out.append("Additional instructions: " + r["instructions"])
    return "\n".join(out)


def take(d, b, sid, t, start_round):
    """Claim the next thing the user sent from the board for this session: a proposal answer, else a request."""
    s = session(d, sid, t)
    for p in sorted(b["proposals"].values(), key=lambda p: p.get("updated", "")):
        if p.get("session") == sid and p.get("status") in ANSWERED and not p.get("delivered"):
            p["delivered"] = t
            if start_round:
                new_round(s, "Board: answered “" + p["title"] + "”", t, "board")
            return reply_text(p) + f"\nAfterwards, start the listener again in the background: `{H} listen`."
    for r in b["requests"]:
        if not r.get("claimed_by") and r.get("session") in (None, "", sid):
            r.update(claimed_by=sid, claimed_at=t, claimed_title=s.get("title", ""))
            s["title"] = s["title"] or auto_title(r["text"])
            if start_round:
                new_round(s, r["text"], t, "board")
            log(s, t, "You (board): " + short(r["text"], 160), "you")
            return request_text(r) + f"\nAfterwards, start the listener again in the background: `{H} listen`."
    return ""


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def listening(b, sid):
    l = b["listeners"].get(sid)
    return bool(l) and age(l.get("at")) < 75


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
        mtime = os.path.getmtime(BOARD) if os.path.exists(BOARD) else 0
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
            seen, beat = os.path.getmtime(BOARD) if os.path.exists(BOARD) else 0, time.time()
        time.sleep(1)
    with locked() as (d, b):
        b["listeners"].pop(sid, None)
    print(f"Nothing came from the board in {LISTEN_HOURS} hours, so the listener stopped. Start it again if the user is still around.")


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


# ---------------------------------------------------------------- entry points

def phone(on):
    path = os.path.join(DIR, "lan.json")
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
        return print(f"Phone access is off. The board is back to this computer only: http://localhost:{PORT}")
    ip = lan_ip()
    if not ip:
        return print("Couldn't find this computer's Wi-Fi address. Is it on a network?")
    print(f"Phone access is on. Open this on a phone on the same Wi-Fi (the key is in the link; it's kept as a cookie after the first visit):\n"
          f"http://{ip}:{PORT}/?key={lan_token()}\n"
          f"Anyone with this link on your network can read the board and send Claude requests. Turn it off with `{H} phone off`.")


def snapshot(out):
    """The page with all its data baked in: read-only, no server needed (for a claude.ai page or a file)."""
    with locked() as (d, b):
        snap = {"progress": d, "board": {**b, "listeners": {}, "requests": []}, "at": now(),
                "history": open(HIST).read() if os.path.exists(HIST) else "", "map": read_json(os.path.join(DIR, "map", "graph.json"), None)}
    html = open(os.path.join(SKILL, "index.html")).read()
    blob = json.dumps(snap).replace("</", "<\\/")
    marker = "<script>\nconst $ ="
    if marker not in html:
        raise ValueError("index.html changed: snapshot marker not found")
    html = html.replace(marker, f"<script>window.SNAPSHOT = {blob};</script>\n{marker}", 1)
    with open(out, "w") as f:
        f.write(html)
    print(f"Snapshot written to {out} ({len(html) // 1024} KB). It contains prompts, plans, replies and file paths from this project.")


def cli(args):
    cmd = args[0]
    if cmd == "serve":
        return serve()
    if cmd == "phone":
        return phone((args[1:] or ["on"])[0] != "off")
    if cmd == "push":
        st = read_json(BOARD, {}).get("settings") or {}
        if st.get("ntfy_topic") and len(args) >= 3:
            send_push(st, args[1], args[2])
        return
    if cmd == "map":
        import codemap, mapapp
        codemap.setup(ROOT, DIR)
        return mapapp.cli(args[1:]) or codemap.cli(args[1:])
    if cmd == "snapshot":
        return snapshot(os.path.abspath(args[1]) if len(args) > 1 else os.path.join(DIR, "snapshot.html"))
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        sys.exit("project-plan: CLAUDE_CODE_SESSION_ID isn't set; run this from a Claude Code session.")
    if cmd == "listen":
        return listen(sid)
    if cmd == "propose":
        revise, spec = load_spec(args[1:])
        with locked() as (d, b):
            pid = propose(d, b, sid, spec, revise, now())
        push("Claude wants your input", spec["title"])
        print(f"Proposal {pid} is on the board: http://localhost:{PORT}/#proposals\n"
              f"Tell the user it's there, make sure the listener is running in the background (`{H} listen`), and stop: "
              "their answer arrives through it. Don't build until they approve.")
        return
    if cmd == "proposal":
        if len(args) < 3 or args[2] not in ("building", "done", "dismissed", "awaiting"):
            sys.exit("usage: hook.py proposal <ID> building|done|dismissed|awaiting")
        with locked() as (d, b):
            p = b["proposals"].get(args[1])
            if not p:
                sys.exit(f"project-plan: no proposal {args[1]}")
            p.update(status=args[2], updated=now())
            if args[2] == "building":
                p["delivered"] = p.get("delivered") or now()
            s = session(d, sid, now())
            if s.get("round") and p["id"] not in s["round"]["proposals"]:
                s["round"]["proposals"].append(p["id"])
        print(f"{args[1]} is now {args[2]}.")
        return
    if cmd in ("plan", "add", "task"):
        with locked() as (d, b):
            apply(d, {"session_id": sid, "hook_event_name": "CLI", "args": args, "cwd": ROOT})
        return
    sys.exit(__doc__)


def main():
    args = sys.argv[1:]
    if os.environ.get("PROJECT_PLAN_OFF") and not args:
        return  # a helper run of claude -p (Ask the map): not a session to show on the board
    if args[:1] == ["hub"]:
        return hub()
    if args[:1] == ["check-update"]:
        u = check_update()
        return print(update_text({"current": APP_VERSION, "latest": u["latest"]}) if APP_VERSION and u.get("latest") and vtuple(u["latest"]) > vtuple(APP_VERSION)
                     else f"project-plan {APP_VERSION or '(hand install)'} · latest {u.get('latest') or 'unknown'}{' · ' + u['error'] if u.get('error') else ''}")
    ev = None if args else json.load(sys.stdin)
    root = find_project(os.environ.get("CLAUDE_PROJECT_DIR") or (ev or {}).get("cwd") or os.getcwd())
    if not root:
        if args:
            sys.exit("project-plan: no .progress/ folder here. Run /project-plan in this project first.")
        return  # project not set up for the board: stay silent
    use_project(root)
    if args:
        return cli(args)
    if not IS_PLUGIN and plugin_enabled():
        return  # a hand install's project hooks: the plugin's hooks already handle this event

    name, sid = ev.get("hook_event_name"), ev.get("session_id", "unknown")
    if name == "SessionStart":
        ensure_server()
        if not os.path.exists(os.path.join(DIR, "map", "graph.json")):  # every project gets a map (folders only) for free
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "map", "update", "--quiet"], cwd=ROOT,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    with locked() as (d, b):
        had_needs = bool(d["sessions"].get(sid, {}).get("needs"))
        s = apply(d, ev)
        sweep(d, b, now())
        # Anything the user sent from the board that the listener hasn't delivered yet.
        msg = take(d, b, sid, now(), name == "Stop") if name in ("UserPromptSubmit", "Stop") else ""
        on = listening(b, sid)
        notes = budget_notes(d, b, s) if name in ("UserPromptSubmit", "PostToolUse") else []
        n = s.get("needs") or {}
    title = s.get("name") or s.get("title") or "A session"
    if name in ("Stop", "SessionEnd") and os.path.isdir(os.path.join(DIR, "map")):  # the free rescan; describing is on request
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "map", "update", "--if-stale"], cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    if n and not had_needs:
        push(f"Needs you: {title}", n.get("what", ""))
    elif name == "Stop" and not msg:
        push(f"Done: {title}", s.get("reply", ""))

    out = None
    if name == "PermissionRequest" and n.get("until"):
        dec = wait_permit(sid, n["id"], time.time() + PERMIT_WAIT)
        if dec:
            decision = {"behavior": "allow"} if dec == "allow" else {"behavior": "deny", "message": "The user denied this on the project board."}
            out = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}
        else:
            with locked() as (d, b):
                x = d["sessions"].get(sid, {}).get("needs")
                if x and x.get("id") == n["id"]:
                    x.pop("until", None)  # not answered on the board: it's the terminal's now
    if name == "UserPromptSubmit":
        ctx = [msg] if msg else []
        if not any(x["status"] in ("pending", "in_progress") for x in s["tasks"]):
            ctx.append(reminder())
        if not on:
            ctx.append(LISTEN_HINT)
        ctx += notes
        if ctx:
            out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "\n\n".join(ctx)}}
    elif name == "PostToolUse" and notes:
        out = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "\n\n".join(notes)}}
    elif name == "Stop" and msg:
        out = {"decision": "block", "reason": msg}  # keep going: the user answered on the board meanwhile
    elif name == "SessionStart":
        ctx = [f"This project has a progress board at http://localhost:{PORT}. " + LISTEN_HINT]
        if IS_PLUGIN:  # the plugin writes nothing into CLAUDE.md: the rules come from here, and again after each compaction
            from setup import RULES
            ctx.append(RULES.format(start="", end="", port=PORT).strip())
        u = update_info()
        if u:
            ctx.append("Tell the user once, in one line: " + update_text(u))
        out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n\n".join(ctx)}}
    if out:
        print(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        if len(sys.argv) > 1:  # CLI: say what went wrong; hooks: stay silent
            sys.exit(f"project-plan: {e}")
    sys.exit(0)
