"""The project map: how the code fits together, for the board's Map view and for Claude.

A free scan (no tokens) reads every file git knows about and pulls out imports, class references,
server endpoints, the requests the code makes, and a fingerprint per file. Claude writes
.progress/map/architecture.json once (the parts of the app, in its own words); `build` then assigns
files to parts, connects them, runs the health check and writes .progress/map/graph.json.

    map update             rescan and rebuild (free; also runs when a session stops)
    map worksheet [PART]   what Claude reads to write or refresh architecture.json
    map described          after editing architecture.json: check it, remember what was described, rebuild
    map overview           the parts and how they connect
    map find WORDS         parts, files and endpoints matching the words
    map show THING         one part, file or endpoint in detail
    map ask "QUESTION"     answer a question about the code (runs claude -p, read-only)
"""
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = MAP = None
H = "python3 ~/.claude/project-plan/hook.py"
MAX_FILES = 6000
MAX_BYTES = 400_000
CODE = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".astro", ".php", ".py", ".go", ".rb", ".rs",
        ".java", ".kt", ".cs", ".swift"}
STYLE = {".css", ".scss", ".sass", ".less", ".html", ".twig", ".blade.php"}
TEXT = CODE | STYLE | {".json", ".yml", ".yaml", ".toml", ".ini", ".env", ".xml", ".sh", ".md", ".txt", ".sql", ".conf", ".properties"}
SKIP_DIRS = ("node_modules/", "vendor/", "build/", "dist/", ".next/", ".nuxt/", "out/", "coverage/", ".git/", ".progress/",
             "__pycache__/", ".venv/", "venv/", "target/", ".svelte-kit/", ".turbo/", ".cache/")
SKIP_FILES = re.compile(r"(\.min\.(js|css)|\.map|\.lock|lock\.json|lock\.yaml|\.pot|\.po|\.mo|\.snap)$")
JS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".astro"}
KINDS = ("screen", "server", "data", "service", "tool", "other")


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


# ---------------------------------------------------------------- listing

def git(*args):
    try:
        r = subprocess.run(["git", "-C", ROOT, *args], capture_output=True, timeout=30)
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def list_files():
    """Every file git knows about (tracked, or new and not ignored); a plain walk outside git."""
    out = git("ls-files", "-z", "-co", "--exclude-standard")
    if out is not None:
        paths = [p for p in out.split("\0") if p]
    else:
        paths = []
        for d, dirs, names in os.walk(ROOT):
            r = os.path.relpath(d, ROOT)
            dirs[:] = [x for x in dirs if not (x.startswith(".") or (x + "/") in SKIP_DIRS)]
            paths += [os.path.normpath(os.path.join(r, n)) for n in names]
    keep = []
    for p in sorted(set(paths)):
        if any(("/" + p).find("/" + s) >= 0 for s in SKIP_DIRS) or SKIP_FILES.search(p) or ext(p) not in TEXT and not os.path.basename(p).startswith(".env"):
            continue
        keep.append(p)
    return keep[:MAX_FILES]


def tracked():
    out = git("ls-files", "-z")
    return set(p for p in (out or "").split("\0") if p)


# ---------------------------------------------------------------- extraction

STR = r"""(?:'((?:[^'\\\n]|\\.)*)'|"((?:[^"\\\n]|\\.)*)")"""
JS_IMPORT = re.compile(r"""(?:\bimport\s[^'"`;]*?\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*|\bexport\s[^'"`;]*?\bfrom\s*)['"]([^'"\n]+)['"]""")
JS_DEF = re.compile(r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function\*?|class|const|let|var)\s+(\w+)", re.M)
JS_FN = re.compile(r"^(?:async\s+)?(?:function\*?|class)\s+(\w+)", re.M)
JS_CONST = re.compile(r"""\b(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:[\w$.]+\s*\|\|\s*)?(`(?:[^`\\]|\\.)*`|'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*")""")
JS_STRINGS = re.compile(r"""`((?:[^`\\]|\\.)*)`|'((?:[^'\\\n]|\\.)*)'|"((?:[^"\\\n]|\\.)*)\"""")
EXPRESS = re.compile(r"""\b(app|router|server|fastify|api|route|routes|\w*[Rr]outer)\.(get|post|put|patch|delete|all|head|options)\s*\(\s*(['"`])(/[^'"`]*)\3""")
NEXT_METHODS = re.compile(r"^\s*export\s+(?:async\s+)?(?:function|const)\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b", re.M)
FETCH = re.compile(r"""\b(fetch|axios(?:\.(get|post|put|patch|delete))?|\$\.(?:ajax|get|post)|ky(?:\.(get|post|put|patch|delete))?)\s*\(\s*(`(?:[^`\\]|\\.)*`|'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*")""")
APIFETCH = re.compile(r"""\bapiFetch\s*\(\s*\{([^{}]|\{[^{}]*\})*?\}\s*\)""", re.S)
METHOD = re.compile(r"""\bmethod\s*:\s*['"](\w+)['"]""", re.I)
URL = re.compile(r"""https?://([a-z0-9][a-z0-9.-]*\.[a-z]{2,})(?::\d+)?[/'"`\s?]""", re.I)

PHP_CLASS = re.compile(r"^\s*(?:(?:final|abstract|readonly)\s+)*(class|interface|trait|enum)\s+(\w+)", re.M)
PHP_FUNC = re.compile(r"^\s*(?:(?:public|protected|private|static|final|abstract)\s+)*function\s+&?(\w+)", re.M)
PHP_NS = re.compile(r"^\s*namespace\s+([\w\\]+)\s*;", re.M)
PHP_REF = re.compile(r"""(?:\bnew\s+\\?([\w\\]+)|\b([A-Z][\w\\]*)::(?!class\b)|\b(?:extends|implements|instanceof)\s+\\?([\w\\]+)|^\s*use\s+\\?([\w\\]+)(?:\s+as\s+\w+)?\s*;)""", re.M)
PHP_INCLUDE = re.compile(r"""\b(?:require|include)(?:_once)?\b\s*\(?([^;]+);""")
PHP_CONST = re.compile(r"""\bconst\s+(\w+)\s*=\s*""" + STR)
PHP_DEFINE = re.compile(r"""\bdefine\s*\(\s*['"](\w+)['"]\s*,\s*""" + STR)
WP_ROUTE = re.compile(r"""\bregister_rest_route\s*\(""")
WP_AJAX = re.compile(r"""add_action\s*\(\s*['"]wp_ajax_(nopriv_)?(\w+)['"]""")
LARAVEL = re.compile(r"""\bRoute::(get|post|put|patch|delete|any|match)\s*\(\s*""" + STR)
WP_METHODS = {"READABLE": ["GET"], "CREATABLE": ["POST"], "EDITABLE": ["POST", "PUT", "PATCH"], "DELETABLE": ["DELETE"], "ALLMETHODS": ["ANY"]}

PY_IMPORT = re.compile(r"^\s*(?:from\s+(\.*[\w.]*)\s+import\s+([\w*, ()]+)|import\s+([\w., ]+))", re.M)
PY_DEF = re.compile(r"^(?:async\s+)?(?:def|class)\s+(\w+)", re.M)
PY_ROUTE = re.compile(r"""@\w+\.(route|get|post|put|patch|delete|api_route)\s*\(\s*""" + STR + r"""([^)]*)""")
PY_REQ = re.compile(r"""\b(?:requests|httpx|session|client)\.(get|post|put|patch|delete)\s*\(\s*f?""" + STR)
DJANGO = re.compile(r"""\b(?:re_)?path\s*\(\s*r?""" + STR)
GO_ROUTE = re.compile(r"""\.(HandleFunc|Handle|GET|POST|PUT|PATCH|DELETE|Get|Post|Put|Patch|Delete)\s*\(\s*"(/[^"]*)\"""")


def lineno(text, pos):
    return text.count("\n", 0, pos) + 1


def lit(m, start=0, end=None):
    """The first string-literal group that matched, from groups[start:end]."""
    return next((g for g in m.groups()[start:end] if g is not None), "")


def php_expr(expr, consts, text="", depth=0):
    """Evaluate a PHP string expression like `'/' . self::BASE . '/(?P<id>\\d+)'` as far as it can."""
    out = []
    for part in re.split(r"\s*\.\s*(?=(?:[^'\"]|'[^']*'|\"[^\"]*\")*$)", expr.strip()):
        m = re.fullmatch(r"\s*" + STR + r"\s*", part)
        if m:
            out.append(lit(m))
            continue
        v = re.fullmatch(r"\s*\$(\w+)\s*", part)
        a = v and depth < 3 and re.search(r"\$" + v.group(1) + r"\s*=\s*([^;=][^;]*);", text)
        if a:  # a local variable assigned once above, like $base = '/' . self::BASE;
            out.append(php_expr(a.group(1), consts, text, depth + 1))
            continue
        c = re.fullmatch(r"\s*(?:[\w\\]+::)?(\w+)\s*", part)
        out.append(consts.get(c.group(1), "*") if c else "*")
    return "".join(out)


def call_args(text, start):
    """The argument text of a call whose '(' is at text[start-1], up to its matching ')'."""
    depth, i, q = 1, start, None
    while i < len(text) and depth:
        ch = text[i]
        if q:
            if ch == "\\":
                i += 1
            elif ch == q:
                q = None
        elif ch in "'\"":
            q = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        i += 1
    return text[start:i - 1]


def split_top(s):
    """Split call arguments at top-level commas."""
    parts, depth, cur, q, i = [], 0, "", None, 0
    while i < len(s):
        ch = s[i]
        if q:
            cur += ch
            if ch == "\\" and i + 1 < len(s):
                cur += s[i + 1]
                i += 1
            elif ch == q:
                q = None
        elif ch in "'\"":
            q = ch
            cur += ch
        elif ch in "([{":
            depth += 1
            cur += ch
        elif ch in ")]}":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
        i += 1
    if cur.strip():
        parts.append(cur)
    return parts


def extract(path, text):
    """What one file defines, uses, serves and calls."""
    e = ext(path)
    f = {"lines": text.count("\n") + 1, "defs": [], "imports": [], "refs": [], "pkgs": [], "endpoints": [], "requests": [],
         "strings": [], "hosts": [], "consts": {}}
    if e in JS:
        for m in JS_IMPORT.finditer(text):
            spec = m.group(1)
            (f["imports"] if spec.startswith((".", "/", "@/", "~/")) else f["pkgs"]).append(spec)
        f["defs"] = list(dict.fromkeys(JS_DEF.findall(text) + JS_FN.findall(text)))
        for m in JS_CONST.finditer(text):
            f["consts"][m.group(1)] = m.group(2)[1:-1]
        for m in EXPRESS.finditer(text):
            f["endpoints"].append({"method": m.group(2).upper().replace("ALL", "ANY"), "path": m.group(4), "line": lineno(text, m.start())})
        nxt = re.search(r"(?:^|/)app/(.*)/route\.(?:ts|js|mjs)$", path) or re.search(r"(?:^|/)pages/(api/.*?)(?:/index)?\.(?:ts|js|tsx|jsx)$", path)
        if nxt:
            route = "/" + re.sub(r"\[\.{3}?(\w+)\]", r"*", re.sub(r"\([^)]*\)/?", "", nxt.group(1)))
            for m in (NEXT_METHODS.finditer(text) if "route." in path else [None]):
                f["endpoints"].append({"method": m.group(1) if m else "ANY", "path": route, "line": lineno(text, m.start()) if m else 1})
        for m in FETCH.finditer(text):
            tail = text[m.end():m.end() + 300]
            meth = (m.group(2) or m.group(3) or (METHOD.search(tail).group(1) if METHOD.search(tail) else "")).upper()
            f["requests"].append({"method": meth, "raw": m.group(4)[1:-1], "line": lineno(text, m.start())})
        for m in APIFETCH.finditer(text):
            body = m.group(0)
            pm = re.search(r"""\bpath\s*:\s*(?:\w+\s*\(\s*)?(`(?:[^`\\]|\\.)*`|'[^'\n]*'|"[^"\n]*"|[A-Za-z_$][\w$]*)""", body)
            mm = METHOD.search(body)
            if pm:
                raw = pm.group(1)
                f["requests"].append({"method": (mm.group(1) if mm else "GET").upper(), "raw": raw[1:-1] if raw[0] in "'\"`" else "${" + raw + "}",
                                      "line": lineno(text, m.start())})
        for m in JS_STRINGS.finditer(text):
            s = next(g for g in m.groups() if g is not None)
            if s.startswith("/") and s.count("/") >= 2 and len(s) < 200 and " " not in s or "${" in s and "/" in s and len(s) < 200:
                f["strings"].append(s)
        f["refs"] = sorted(set(re.findall(r"\b([A-Z][A-Z0-9_]{3,})\b", text)))  # constant names: paths built from shared constants
    elif e == ".php" or e == ".blade.php":
        ns = PHP_NS.search(text)
        f["ns"] = ns.group(1) if ns else ""
        f["defs"] = [m.group(2) for m in PHP_CLASS.finditer(text)] + list(dict.fromkeys(PHP_FUNC.findall(text)))[:40]
        f["classes"] = [m.group(2) for m in PHP_CLASS.finditer(text)]
        for m in PHP_CONST.finditer(text):
            f["consts"][m.group(1)] = lit(m, 1)
        for m in PHP_DEFINE.finditer(text):
            f["consts"][m.group(1)] = lit(m, 1)
        refs = set()
        for m in PHP_REF.finditer(text):
            name = next(g for g in m.groups() if g)
            last = name.strip("\\").split("\\")[-1]
            if last.lower() not in ("self", "static", "parent", "this") and last[:1].isupper():
                refs.add(last)
        f["refs"] = sorted(refs - set(f["classes"]))
        for m in PHP_INCLUDE.finditer(text):
            s = re.findall(r"""['"]([^'"]+\.php)['"]""", m.group(1))
            if s:
                f["imports"].append(("__DIR__" in m.group(1) or "dirname" in m.group(1)) and "./" + s[-1].lstrip("/") or s[-1])
        for m in WP_ROUTE.finditer(text):
            args = split_top(call_args(text, m.end()))
            if len(args) < 2:
                continue
            f["endpoints"].append({"wp": [args[0], args[1]], "args": ",".join(args[2:])[:4000], "line": lineno(text, m.start())})
        for m in WP_AJAX.finditer(text):
            f["endpoints"].append({"method": "AJAX", "path": "/wp-admin/admin-ajax.php?action=" + m.group(2), "public": bool(m.group(1)), "line": lineno(text, m.start())})
        for m in LARAVEL.finditer(text):
            f["endpoints"].append({"method": m.group(1).upper().replace("MATCH", "ANY"), "path": "/" + lit(m, 1).lstrip("/"), "line": lineno(text, m.start())})
        for m in re.finditer(r"\bwp_(?:safe_)?remote_(get|post|request)\s*\(\s*" + STR, text):
            f["requests"].append({"method": m.group(1).upper().replace("REQUEST", ""), "raw": lit(m, 1), "line": lineno(text, m.start())})
    elif e == ".py":
        for m in PY_IMPORT.finditer(text):
            if m.group(1) is not None:
                mod = m.group(1)
                f["imports"].append(mod if mod.strip(".") else mod + "|" + m.group(2))  # `from . import x` names modules
            else:
                f["imports"] += [x.strip().split(" as ")[0] for x in m.group(3).split(",") if x.strip()]
        f["defs"] = PY_DEF.findall(text)
        for m in PY_ROUTE.finditer(text):
            route = lit(m, 1, 3)
            verb = m.group(1).upper()
            meths = re.findall(r"['\"](GET|POST|PUT|PATCH|DELETE)['\"]", m.group(4) or "") if verb in ("ROUTE", "API_ROUTE") else [verb]
            for meth in meths or ["GET"]:
                f["endpoints"].append({"method": meth, "path": route, "line": lineno(text, m.start())})
        if path.endswith("urls.py"):
            for m in DJANGO.finditer(text):
                f["endpoints"].append({"method": "ANY", "path": "/" + lit(m).lstrip("^").rstrip("$"), "line": lineno(text, m.start())})
        for m in PY_REQ.finditer(text):
            f["requests"].append({"method": m.group(1).upper(), "raw": lit(m, 1), "line": lineno(text, m.start())})
    elif e == ".go":
        f["defs"] = re.findall(r"^func\s+(?:\([^)]*\)\s*)?(\w+)", text, re.M)
        for m in GO_ROUTE.finditer(text):
            meth = m.group(1).upper()
            f["endpoints"].append({"method": "ANY" if meth.startswith("HANDLE") else meth, "path": m.group(2), "line": lineno(text, m.start())})
    elif e in CODE:
        f["defs"] = re.findall(r"^\s*(?:pub\s+)?(?:def|class|fn|struct|func|interface|module)\s+(\w+)", text, re.M)[:40]
    if e in CODE:
        f["hosts"] = sorted({h.lower() for h in URL.findall(text)} - {"localhost", "127.0.0.1", "example.com", "www.w3.org", "schema.org", "github.com", "www.example.com"})
    return f


# ---------------------------------------------------------------- resolution

def resolve_js(src, spec, files, aliases):
    if spec.startswith(("@/", "~/")):
        cands = [a + spec[2:] for a in aliases]
    elif spec.startswith("/"):
        cands = [spec.lstrip("/")]
    else:
        cands = [os.path.normpath(os.path.join(os.path.dirname(src), spec))]
    for c in cands:
        c = c.split("?")[0]
        for x in [c] + [c + e for e in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".scss", ".css")] + \
                 [c + "/index" + e for e in (".js", ".jsx", ".ts", ".tsx", ".mjs")]:
            if x in files:
                return x
    return None


def resolve_php(src, spec, files):
    s = spec[2:] if spec.startswith("./") else spec
    for c in (os.path.normpath(os.path.join(os.path.dirname(src), s)), os.path.normpath(s.lstrip("/"))):
        if c in files:
            return c
    tail = [p for p in files if p.endswith("/" + s.lstrip("/"))]
    return tail[0] if len(tail) == 1 else None


def resolve_py(src, spec, files, pkgs):
    names = []
    if "|" in spec:
        spec, names = spec.split("|", 1)
        names = [n.strip().split(" as ")[0] for n in names.strip("() ").split(",")]
    if spec.startswith("."):
        dots = len(spec) - len(spec.lstrip("."))
        base = os.path.dirname(src)
        for _ in range(dots - 1):
            base = os.path.dirname(base)
        rest = spec.lstrip(".").replace(".", "/")
        stems = [os.path.join(base, rest)] if rest else [os.path.join(base, n) for n in names]
    else:
        stems = [os.path.join(p, spec.replace(".", "/")) for p in pkgs]
    out = []
    for st in stems:
        st = os.path.normpath(st)
        for c in (st + ".py", st + "/__init__.py"):
            if c in files:
                out.append(c)
                break
    return out


def segs(path):
    p = re.sub(r"\(\?P<\w+>[^)]*\)|\{[^}]*\}|<[^>]+>|:\w+|\[[^\]]+\]", "*", path.split("?")[0].split("#")[0])
    return [s for s in p.strip("/").split("/") if s]


def match_score(cand, ep):
    """How well a request path (with * for unknown bits) fits an endpoint path; 0 = no match."""
    best = 0
    for k in range(len(cand)):
        sub = cand[k:]
        glued = sub and sub[-1].endswith("*") and sub[-1] != "*"
        if len(sub) != len(ep) and not (glued and len(ep) >= len(sub)):
            continue
        lits = 0
        for i, s in enumerate(sub):
            e = ep[i]
            if s == "*" or e == "*":
                continue
            if s == e or "*" in s and fnmatch.fnmatchcase(e, s):
                lits += 1
            else:
                lits = -1
                break
        if lits >= 2 or lits >= 1 and len(ep) == 1 and len(sub) == 1:
            best = max(best, lits + (1 if len(sub) == len(ep) else 0))
    return best


def eval_js(raw, consts, depth=0):
    def sub(m):
        expr = m.group(1).strip()
        if re.fullmatch(r"[A-Za-z_$][\w$]*", expr) and expr in consts and depth < 4:
            return eval_js(consts[expr], consts, depth + 1)
        return "*"
    return re.sub(r"\$\{([^{}]*)\}", sub, raw)


# ---------------------------------------------------------------- scan

def scan():
    paths = list_files()
    files, texts = {}, {}
    for p in paths:
        try:
            full = os.path.join(ROOT, p)
            if os.path.getsize(full) > MAX_BYTES:
                files[p] = {"lines": 0, "big": True, "hash": "", "sig": ""}
                continue
            with open(full, "rb") as fh:
                raw = fh.read()
            if b"\0" in raw[:4096]:
                continue
            text = raw.decode("utf-8", "replace")
        except OSError:
            continue
        texts[p] = text
        if ext(p) in CODE or ext(p) in STYLE:
            f = extract(p, text)
        else:
            f = {"lines": text.count("\n") + 1, "config": True}
        f["hash"] = hashlib.sha1(raw).hexdigest()[:12]
        files[p] = f
    code = {p for p, f in files.items() if not f.get("config")}

    # Constants shared across files (JS path constants, PHP class constants).
    js_consts, php_consts = {}, {}
    for p, f in files.items():
        for k, v in (f.get("consts") or {}).items():
            (php_consts if ext(p) == ".php" else js_consts).setdefault(k, v)

    # Endpoints with their full paths and who may call them.
    endpoints = {}
    for p, f in files.items():
        for ep in f.get("endpoints", []):
            if "wp" in ep:
                consts = {**php_consts, **(f.get("consts") or {})}  # the class's own constants first
                ns = php_expr(ep["wp"][0], consts, texts.get(p, "")).strip("/")
                route = php_expr(ep["wp"][1], consts, texts.get(p, ""))
                path = "/" + ns + "/" + route.lstrip("/")
                args = ep["args"]
                found = list(re.finditer(r"""['"]methods['"]\s*=>\s*(?:\\?WP_REST_Server::(\w+)|""" + STR + ")", args)) or [None]
                for i, m in enumerate(found):  # each handler array has its own methods and permission_callback
                    seg = args[m.start() if m else 0:found[i + 1].start() if i + 1 < len(found) else len(args)]
                    back = args[found[i - 1].end() if i else 0:m.start()] if m else ""
                    perm = re.search(r"""['"]permission_callback['"]\s*=>\s*([^\n]+)""", seg) or re.search(r"""['"]permission_callback['"]\s*=>\s*([^\n]+)""", back[-600:] if "array" not in back[-600:].split("permission_callback")[-1] else "")
                    meths = (WP_METHODS.get(m.group(1), []) if m.group(1) else [x.strip().upper() for x in lit(m, 1).split(",")]) if m else ["GET"]
                    public = bool(perm) and "__return_true" in perm.group(1)
                    for meth in meths:
                        endpoints[f"{meth} {path}"] = {"method": meth, "path": path, "file": p, "line": ep["line"], "public": public,
                                                       "checked": bool(perm), "nonce": bool(re.search(r"nonce", texts.get(p, ""), re.I))}
            else:
                key = f"{ep['method']} {ep['path']}"
                endpoints.setdefault(key, {"method": ep["method"], "path": ep["path"], "file": p, "line": ep["line"], "public": ep.get("public")})
            f.pop("args", None)
        f.pop("endpoints", None)
    ep_segs = {k: segs(v["path"]) for k, v in endpoints.items()}

    # Imports and references between files.
    classes = {}
    for p, f in files.items():
        for c in f.get("classes", []):
            classes.setdefault(c, []).append(p)
    aliases = [a for a in ("src/", "app/", "") if any(x.startswith(a) for x in code)]
    pypkgs = [""] + sorted({p.split("/")[0] + "/" for p in code if p.endswith(".py") and "/" in p} - {"tests/"})
    for p, f in files.items():
        if f.get("config"):
            continue
        uses = set()
        e = ext(p)
        for spec in f.get("imports", []):
            if e in JS:
                r = resolve_js(p, spec, files, aliases)
                if r:
                    uses.add(r)
            elif e == ".php":
                r = resolve_php(p, spec, files)
                if r:
                    uses.add(r)
            elif e == ".py":
                uses.update(resolve_py(p, spec, files, pypkgs))
        if e == ".php":
            for c in f.get("refs", []):
                hit = classes.get(c, [])
                if len(hit) == 1:
                    uses.add(hit[0])
        uses.discard(p)
        f["uses"] = sorted(uses)

        # Requests matched to endpoints: explicit calls, then path-like strings and shared path constants.
        reqs, seen = [], set()
        cands = [(r.get("method", ""), eval_js(r["raw"], {**js_consts, **f.get("consts", {})}), r["line"]) for r in f.get("requests", [])]
        own = set((f.get("consts") or {}).values())  # a file that only defines path constants doesn't call them
        cands += [("", eval_js(s, {**js_consts, **f.get("consts", {})}), 0) for s in f.get("strings", []) if s not in own]
        cands += [("", eval_js(js_consts[r], js_consts), 0) for r in f.get("refs", []) if r in js_consts and "/" in js_consts[r] and r not in (f.get("consts") or {})]
        for meth, url, line in cands:
            if url.startswith("http") and "/wp-json/" not in url:
                continue
            cs = segs(url.split("/wp-json", 1)[-1])
            scored = [(match_score(cs, es), k) for k, es in ep_segs.items()
                      if not meth or endpoints[k]["method"] in (meth, "ANY") or meth == "ANY"]
            top = max((s for s, _ in scored), default=0)
            for s, k in scored:
                if s and s == top and k not in seen and endpoints[k]["file"] != p:
                    seen.add(k)
                    reqs.append({"to": k, "line": line})
        f["calls"] = reqs
        for k in ("imports", "strings", "requests", "consts", "refs", "classes"):
            f.pop(k, None)
        f["sig"] = hashlib.sha1(json.dumps([f.get("defs"), [r["to"] for r in reqs], f.get("ns", "")], sort_keys=True).encode()).hexdigest()[:12]
    return {"scanned": now(), "files": files, "endpoints": endpoints, "_texts": texts}


# ---------------------------------------------------------------- health

SECRET = [
    (r"\bsk_live_[0-9a-zA-Z]{16,}", "a Stripe live secret key"),
    (r"\brk_live_[0-9a-zA-Z]{16,}", "a Stripe restricted key"),
    (r"\bAKIA[0-9A-Z]{16}\b", "an AWS access key"),
    (r"\bgh[pousr]_[A-Za-z0-9]{30,}", "a GitHub token"),
    (r"\bxox[abprs]-[A-Za-z0-9-]{10,}", "a Slack token"),
    (r"\bAIza[0-9A-Za-z_-]{35}\b", "a Google API key"),
    (r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{32,}", "an OpenAI or Anthropic API key"),
    (r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----", "a private key"),
    (r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}", "a SendGrid API key"),
    (r"""(?i)(?:\b|_)(?:api[_-]?key|secret|password|passwd|auth[_-]?token|access[_-]?token|client[_-]?secret)\b['"]?\s*(?:=>|[:=])\s*['"]([^'"\s]{12,})['"]""", "a hard-coded secret"),
]
PLACEHOLDER = re.compile(r"(?i)x{4,}|your[_-]|example|changeme|placeholder|dummy|test|sample|\*{3,}|<[^>]+>|\$\{|%s|^\w+_(?:key|secret|token)$|__\w+__")


def health(sc, tracked_files):
    texts, files, issues = sc["_texts"], sc["files"], []

    def add(rule, sev, title, detail, fix, locs):
        if locs:
            ident = rule + ":" + hashlib.md5("|".join(sorted({l["file"] for l in locs})).encode()).hexdigest()[:8]
            issues.append({"id": ident, "rule": rule, "severity": sev, "title": title, "detail": detail, "fix": fix, "locations": locs[:25]})

    def grep(pattern, only=None, skip_tests=False, flags=0):
        rx, out = re.compile(pattern, flags), []
        for p, t in texts.items():
            if only and ext(p) not in only or skip_tests and re.search(r"(^|/)(tests?|__tests__|spec|fixtures?)/|\.(test|spec)\.", p):
                continue
            for m in rx.finditer(t):
                out.append({"file": p, "line": lineno(t, m.start()), "text": t[t.rfind("\n", 0, m.start()) + 1:t.find("\n", m.end()) if t.find("\n", m.end()) >= 0 else len(t)].strip()[:160], "m": m})
        return out

    # Secrets in the code.
    found = []
    for pat, what in SECRET:
        for l in grep(pat):
            val = l["m"].group(1) if l["m"].groups() else l["m"].group(0)
            if PLACEHOLDER.search(val) or os.path.basename(l["file"]).startswith(".env") and l["file"] not in tracked_files:
                continue
            l["what"] = what
            if not any(x["file"] == l["file"] and x["line"] == l["line"] for x in found):  # one report per line
                found.append(l)
    add("secret", "high", f"{len(found)} secret{'s' if len(found) != 1 else ''} written into the code",
        "Anything in the repository is readable by everyone who can read the repository, and stays in git history after it's deleted.",
        "Move these secrets out of the code into environment variables or the platform's secret settings, rotate the exposed keys, and make sure the files that hold them are ignored by git.",
        [{k: v for k, v in l.items() if k != "m"} | {"text": l["what"]} for l in found])

    envs = [p for p in tracked_files if re.search(r"(^|/)\.env(\.[\w-]+)?$", p) and not p.endswith((".example", ".sample", ".template", ".dist"))]
    add("env-in-git", "high", "A private .env file is committed to git", "Settings files like .env usually hold passwords and keys.",
        "Remove the committed .env files from git (git rm --cached), add .env to .gitignore, and rotate any secrets they held.",
        [{"file": p, "line": 1, "text": "tracked by git"} for p in envs])

    # Endpoints anyone can call.
    eps = sc["endpoints"]
    nocheck = [v for v in eps.values() if "checked" in v and not v["checked"]]
    add("wp-no-permission", "high", f"{len(nocheck)} REST route{'s have' if len(nocheck) != 1 else ' has'} no permission_callback",
        "WordPress treats a route without permission_callback as public and logs a notice.",
        "Add a permission_callback to each of these register_rest_route calls: current_user_can() for admin routes, or __return_true plus a nonce check and rate limit for routes that are public on purpose.",
        [{"file": v["file"], "line": v["line"], "text": f"{v['method']} {v['path']}"} for v in nocheck])
    writes = [v for v in eps.values() if v.get("public") and v["method"] in ("POST", "PUT", "PATCH", "DELETE", "ANY", "AJAX")]
    loud = [v for v in writes if not v.get("nonce")]
    add("public-write", "medium", f"{len(loud)} public endpoint{'s change' if len(loud) != 1 else ' changes'} data without a nonce check",
        "Anyone on the internet can call these, and nothing in the handler's file checks a nonce or token.",
        "Check each of these public endpoints: if it should be public, verify a nonce or signed token and rate-limit it; otherwise require a logged-in user with the right capability.",
        [{"file": v["file"], "line": v["line"], "text": f"{v['method']} {v['path']}"} for v in loud])
    add("public-by-design", "low", f"{len(writes) - len(loud)} public endpoint{'s' if len(writes) - len(loud) != 1 else ''} change data (nonce checked)",
        "These are open to anyone but their files check a nonce. Fine if that's the plan; worth a look if not.",
        "Review these public endpoints: confirm each verifies its nonce before acting, validates and sanitizes every field, and is rate-limited.",
        [{"file": v["file"], "line": v["line"], "text": f"{v['method']} {v['path']}"} for v in writes if v.get("nonce")])

    # Injection and unsafe input handling.
    def unsafe(l):  # table names ({$table}, {$wpdb->prefix}…) are normal; other variables in the SQL are not
        t = texts[l["file"]]
        call = t[l["m"].start():t.find(";", l["m"].start())]  # just this query call, not the whole line
        if "prepare" in call:
            return False
        for v in re.findall(r"\$(\w+)", call.split("(", 1)[-1]):
            a = re.search(r"\$" + v + r"\s*=\s*([^;]+);", t)
            if v != "wpdb" and not re.search(r"table|prefix", v + (a.group(1) if a else ""), re.I):
                return True
        return False
    sql = [l for l in grep(r"""\$wpdb->(?:query|get_results|get_var|get_row|get_col)\s*\(\s*(?:"[^"]*\$\w|['"][^'"]*['"]\s*\.\s*\$)""", {".php"}) if unsafe(l)]
    sql += grep(r"""\.execute\s*\(\s*(?:f["']|["'][^"']*["']\s*(?:%|\.format|\+))""", {".py"})
    sql += grep(r"""\.(?:query|raw)\s*\(\s*`[^`]*\$\{""", JS)
    add("sql", "medium", f"{len(sql)} database quer{'ies are' if len(sql) != 1 else 'y is'} built from variables", "Values pasted into SQL can change the query (SQL injection).",
        "Rewrite these queries with placeholders ($wpdb->prepare, parameterised queries) instead of pasting variables into the SQL.", sql)
    raw = grep(r"""\b(?:echo|print)\s+\$_(?:GET|POST|REQUEST|COOKIE)\b|\bunserialize\s*\(\s*\$_|\bextract\s*\(\s*\$_|\beval\s*\(\s*\$_""", {".php"})
    add("raw-input", "high", "Request input is echoed or executed directly", "Printing or unserializing request data as-is allows XSS or code execution.",
        "Sanitize and escape the request input at each of these places (sanitize_text_field / esc_html, json_decode instead of unserialize).", raw)
    evals = grep(r"""(?<![\w.])eval\s*\(""", JS | {".php", ".py"}, skip_tests=True)
    add("eval", "medium", f"eval() is used in {len({l['file'] for l in evals})} file{'s' if len({l['file'] for l in evals}) != 1 else ''}", "eval runs any text as code.",
        "Replace these eval() calls with code that doesn't execute strings.", evals)
    html = grep(r"dangerouslySetInnerHTML", JS, skip_tests=True)
    add("raw-html", "low", f"HTML is inserted unescaped in {len({l['file'] for l in html})} place{'s' if len(html) != 1 else ''}", "Fine for trusted content; an XSS risk if any of it comes from users.",
        "Check that every dangerouslySetInnerHTML here only receives trusted or sanitized HTML.", html)

    # Webhooks and CORS.
    hooks = []
    for p, t in texts.items():
        if ext(p) in CODE and re.search(r"stripe", t, re.I) and re.search(r"webhook", t, re.I) and re.search(r"\bpost\b|POST", t) \
                and not re.search(r"constructEvent|construct_event|verify_header|Webhook::constructEvent|stripe-signature|Stripe-Signature", t):
            hooks.append({"file": p, "line": 1, "text": "Stripe webhook without a signature check"})
    add("webhook", "high", "A payment webhook doesn't check the signature", "Anyone could send a fake 'payment succeeded' event.",
        "Verify the Stripe-Signature header with the webhook secret (stripe.webhooks.constructEvent) before trusting these events.", hooks)
    cors = grep(r"""Access-Control-Allow-Origin['"]?\s*[:,]\s*['"]?\*|\bcors\(\s*\)|allow_origins\s*=\s*\[\s*['"]\*""", CODE, skip_tests=True)
    add("cors", "low", "Any website can call the server from a visitor's browser", "CORS is open to every origin.",
        "Limit CORS to the origins that need it.", cors)

    # Housekeeping.
    big = [{"file": p, "line": 1, "text": f"{f['lines']} lines"} for p, f in files.items() if ext(p) in CODE and f.get("lines", 0) > 1200]
    add("large", "low", f"{len(big)} file{'s are' if len(big) != 1 else ' is'} over 1,200 lines", "Big files are slower to read and change, for people and for Claude.",
        "Suggest how to split these large files into smaller modules, without changing behaviour.", big)
    local = grep(r"""['"`]https?://(?:localhost|127\.0\.0\.1)(?::\d+)?""", CODE, skip_tests=True)
    local = [l for l in local if not re.search(r"(config|\.env|vite|webpack|playwright|jest|setup)", l["file"], re.I)]
    add("localhost", "low", "Code points at a localhost address", "These break once the app runs anywhere else.",
        "Replace these hard-coded localhost URLs with configuration.", local)
    for i in issues:
        for l in i["locations"]:
            l.pop("m", None)
    order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda i: (order[i["severity"]], -len(i["locations"])))
    return issues


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


def build(sc=None, quiet=False):
    sc = sc or scan()
    arch = rj(os.path.join(MAP, "architecture.json"), None)
    described = rj(os.path.join(MAP, "described.json"), {})
    dismissed = rj(os.path.join(MAP, "dismissed.json"), {})
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
    for k, v in eps.items():
        pid = owner.get(v["file"])
        v["part"] = pid
        v["desc"] = edesc.get(k, "")
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
            e = edges.setdefault((a, b), {"from": a, "to": b, "imports": 0, "calls": 0, "label": ""})
            if kind == "label":
                e["label"] = why
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
            edge(str(l.get("from")), str(l.get("to")), "label", str(l.get("label", "")) or "uses")

    out_files = {}
    for p, f in files.items():
        if f.get("config") and p not in owner:
            continue
        out_files[p] = {"part": owner.get(p), "desc": fdesc.get(p) or fdesc.get(moved.get(p), ""), "lines": f.get("lines", 0),
                        "defs": (f.get("defs") or [])[:30], "uses": f.get("uses", []), "usedBy": sorted(used_by.get(p, [])),
                        "calls": [c["to"] for c in f.get("calls", [])], "pkgs": sorted(set(f.get("pkgs", [])))[:20],
                        "hosts": f.get("hosts", [])}
        if f.get("config"):
            out_files[p]["config"] = True
    issues = health(sc, tracked())
    for i in issues:
        i["dismissed"] = i["id"] in dismissed
        i["parts"] = sorted({owner.get(l["file"]) for l in i["locations"] if owner.get(l["file"])})
    stale = [p["id"] for p in parts.values() if p["new"] or p["changed"] or p["removed"]]
    unassigned = sorted(p for p in files if p not in owner and not files[p].get("config"))
    graph = {"built": now(), "scanned": sc["scanned"], "auto": auto, "project": os.path.basename(ROOT),
             "style": (arch or {}).get("style", "developer"), "summary": (arch or {}).get("summary", ""),
             "described": (arch or {}).get("described", ""),
             "parts": list(parts.values()), "edges": list(edges.values()), "files": out_files, "endpoints": eps,
             "unassigned": unassigned, "stale": stale,
             "health": {"issues": issues, "high": sum(1 for i in issues if i["severity"] == "high" and not i["dismissed"]),
                        "medium": sum(1 for i in issues if i["severity"] == "medium" and not i["dismissed"]),
                        "low": sum(1 for i in issues if i["severity"] == "low" and not i["dismissed"])}}
    wj(os.path.join(MAP, "graph.json"), graph)
    if not quiet:
        print(summary(graph))
    return graph


def summary(g):
    nf = sum(1 for f in g["files"].values() if not f.get("config"))
    h = g["health"]
    lines = [f"Map of {g['project']}: {len(g['parts'])} parts, {nf} code files, {len(g['endpoints'])} endpoints, {len(g['edges'])} connections."]
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
  "links": [{"from": "notifications", "to": "mailchimp", "label": "POST /3.0/lists/{id}/members"}],
  "files": {"includes/class-rest-responses.php": "POST /responses: validates, rescoring server-side, stores the row."},
  "endpoints": {"POST /ml-quiz/v1/responses": "Public quiz submission; nonce + per-IP rate limit; recomputes the score."}
}"""

RULES = """How to write .progress/map/architecture.json (style: {style}):
- 4 to 12 parts. A part is something a developer would name when explaining the app: a screen or UI area, an API, a domain module (scoring, billing), storage, a job runner, tooling. Don't mirror folders unless the folders are the real boundaries.
- kind: screen (UI the user sees), server (handlers, business logic), data (tables, models, storage), service (outside services: payment, email, CRM; no paths), tool (build, tests, scripts), other.
- paths: globs relative to the project root. `**` crosses folders, `*` doesn't, a folder name means everything under it. The most specific pattern wins, so broad catch-alls are fine. Every code file should land in a part; tests and build tooling can share a "tool" part.
- links: only what the scan can't see (it already follows imports, PHP class references and request → endpoint matches): outside services, webhooks, cron, queues, events. Label with the call or event.
- files: one line per code file, {style_hint}. endpoints: one line per endpoint key as listed, saying who may call it.
- summary text is shown to the user on the board: keep it plain and short; no marketing words.
Write the file with the Write tool (it is the only file in .progress/ you edit by hand), then run `{H} map described`."""


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
            out.append("- " + "; ".join(bits))

    if g["auto"] or arg == "--all":
        out.append(f"# Map worksheet: {g['project']} (whole project)\n")
        out.append(RULES.format(style=style, style_hint=hint, H=H))
        out.append("\nSchema:\n" + SCHEMA)
        out.append("\nRead the README and entry points as needed, then write architecture.json. The scan found:\n")
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
        out.append(f"\nEndpoints ({len(g['endpoints'])}): " + "; ".join(f"{k} in {v['file']}:{v['line']}{' (public)' if v.get('public') else ''}" for k, v in list(g["endpoints"].items())[:80]))
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
    for path, f in g["files"].items():
        hay = f"{path} {f['desc']} {' '.join(f['defs'])}".lower()
        if all(w in hay for w in q):
            hits.append((1, f"file {path} [{f['part'] or 'no part'}]{': ' + f['desc'] if f['desc'] else ''}"))
    for k, v in g["endpoints"].items():
        hay = f"{k} {v.get('desc', '')}".lower()
        if all(w in hay for w in q):
            hits.append((2, f"endpoint {k} → {v['file']}:{v['line']}{': ' + v['desc'] if v.get('desc') else ''}"))
    hits.sort()
    print("\n".join(h for _, h in hits[:25]) or "Nothing in the map matches. Search the code instead.")


def show(thing):
    g = load()
    parts = {p["id"]: p for p in g["parts"]}
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
    f = g["files"].get(thing.lstrip("./"))
    if f:
        out = [f"{thing} · part {f['part'] or '(none)'} · {f['lines']} lines", f["desc"] or "(no description)"]
        for label, key in (("Defines", "defs"), ("Uses", "uses"), ("Used by", "usedBy"), ("Calls", "calls"), ("Packages", "pkgs"), ("Talks to", "hosts")):
            if f.get(key):
                out.append(f"{label}: {', '.join(f[key])}")
        serves = [k for k, v in g["endpoints"].items() if v["file"] == thing]
        if serves:
            out.append("Serves: " + ", ".join(serves))
        return print("\n".join(out))
    eps = [(k, v) for k, v in g["endpoints"].items() if thing.lower() in k.lower()]
    if eps:
        for k, v in eps[:10]:
            print(f"{k} → {v['file']}:{v['line']} [{v.get('part') or 'no part'}]{' · public' if v.get('public') else ''}"
                  f"{chr(10) + '  ' + v['desc'] if v.get('desc') else ''}{chr(10) + '  called from ' + ', '.join(v['callers']) if v.get('callers') else ''}")
        return
    print(f"Nothing called {thing!r} in the map. Try `{H} map find WORDS`.")


# ---------------------------------------------------------------- ask

def claude_bin():
    for c in (shutil.which("claude"), os.path.expanduser("~/.claude/local/claude"), os.path.expanduser("~/.local/bin/claude"),
              "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def ask(question, history=None):
    """Answer a question about the code with claude -p, read-only, in the project. Returns {answer, parts, cost}."""
    exe = claude_bin()
    if not exe:
        return {"error": "Couldn't find the claude command on this computer."}
    g = load()
    prior = "".join(f"\nEarlier question: {h.get('q', '')}\nYour answer: {h.get('a', '')[:1500]}\n" for h in (history or [])[-3:])
    prompt = (f"You answer questions about this codebase for its developer, in a few short paragraphs or a short list (under 250 words). "
              f"Cite code as path:line. Read the code to check before you answer; don't guess.\n\n{overview(g)}\n{prior}\n"
              f"Question: {question}\n\nEnd with one last line listing the map parts your answer involves, exactly like:\n"
              f"PARTS: part-id, part-id\n(valid ids: {', '.join(p['id'] for p in g['parts'])})")
    env = dict(os.environ, PROJECT_PLAN_OFF="1")
    try:
        r = subprocess.run([exe, "-p", prompt, "--output-format", "json", "--allowedTools", "Read,Grep,Glob",
                            "--disallowedTools", "Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch,Agent,Task"],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"error": "Claude took longer than 5 minutes; try a narrower question."}
    try:
        res = json.loads(r.stdout)
    except ValueError:
        return {"error": (r.stderr or r.stdout or "claude -p failed").strip()[-400:]}
    text = (res.get("result") or "").strip()
    if res.get("is_error") or not text:
        return {"error": text or "Claude returned no answer."}
    ids = {p["id"] for p in g["parts"]}
    m = re.search(r"^\s*PARTS:\s*(.*)$", text, re.M)
    parts = [x.strip() for x in m.group(1).split(",") if x.strip() in ids] if m else []
    if m:
        text = (text[:m.start()] + text[m.end():]).strip()
    return {"answer": text, "parts": parts, "cost": res.get("total_cost_usd") or 0, "at": now()}


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
    if cmd == "ask" and rest:
        r = ask(" ".join(rest))
        return print(r.get("error") or f"{r['answer']}\n\nParts: {', '.join(r['parts']) or '-'} · ${r['cost']:.3f}")
    sys.exit(__doc__)
