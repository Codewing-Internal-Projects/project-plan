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
import fnmatch
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from datetime import date, datetime, timezone

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


# ---------------------------------------------------------------- extraction

STR = r"""(?:'((?:[^'\\\n]|\\.)*)'|"((?:[^"\\\n]|\\.)*)")"""
JSLIT = r"""(`(?:[^`\\]|\\.)*`|'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*")"""
ID = r"[A-Za-z_$][\w$]*"
JS_IMPORT = re.compile(r"""(?:\bimport\s[^'"`;]*?\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*|\bnew\s+(?:URL|Worker|SharedWorker)\s*\(\s*|\bexport\s[^'"`;]*?\bfrom\s*)['"]([^'"\n]+)['"]""")
JS_BIND = re.compile(r"""\bimport\s+(?:type\s+)?(?:(""" + ID + r""")\s*,?\s*)?(?:\{([^}]*)\}|\*\s+as\s+(""" + ID + r"""))?\s*from\s*['"]([^'"]+)['"]""")
REQ_BIND = re.compile(r"""\b(?:const|let|var)\s+(?:(""" + ID + r""")|\{([^}]*)\})\s*=\s*require\s*\(\s*['"]([^'"]+)['"]\s*\)""")
JS_DEF = re.compile(r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function\*?|class|const|let|var)\s+(\w+)", re.M)
JS_FN = re.compile(r"^(?:async\s+)?(?:function\*?|class)\s+(\w+)", re.M)
JS_DEFAULT = re.compile(r"^\s*(?:export\s+default\s+(?:async\s+)?(?:function\*?\s+|class\s+)?(\w+)|module\.exports\s*=\s*(\w+)\s*;?\s*$)", re.M)
JS_CONST = re.compile(r"""\b(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:[\w$.]+\s*\|\|\s*)?(`(?:[^`\\]|\\.)*`|'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*")""")
JS_STRINGS = re.compile(r"""`((?:[^`\\]|\\.)*)`|'((?:[^'\\\n]|\\.)*)'|"((?:[^"\\\n]|\\.)*)\"""")
JS_DECL = re.compile(r"\b(?:const|let|var)\s+(" + ID + r")\s*=\s*(?:await\s+)?")
SERVER_RHS = re.compile(r"(express\s*\(|require\(\s*['\"](?:express|fastify|koa)['\"]\s*\)\s*\(|fastify\s*\(|Fastify\s*\(|new\s+(?:Hono|OpenAPIHono|Elysia)\b|polka\s*\(|new\s+Koa\b)|"
                        r"((?:express\s*\.\s*|require\(\s*['\"]express['\"]\s*\)\s*\.\s*)?Router\s*\(|new\s+(?:Koa)?Router\b|createRouter\s*\()")
CLIENT_RHS = re.compile(r"(?:axios|ky|ofetch|\$fetch|wretch|got)\s*\.\s*(?:create|extend)\s*\(")
SERVERY = re.compile(r"^(?:app|router|server|fastify|routes?|\w*[Rr]outer|\w*App|instance)$")
NOT_SERVER = {"axios", "ky", "http", "https", "request", "superagent", "client", "$", "fetcher", "msw", "cy", "page", "wrapper", "this"}
ROUTE_VERB = re.compile(r"\b(" + ID + r")\s*\.\s*(get|post|put|patch|delete|all|head|options)\s*\(\s*(['\"`])(/[^'\"`]*)\3")
MOUNT = re.compile(r"\b(" + ID + r")\s*\.\s*(use|route|register)\s*\(")
SERVERISH_TEXT = re.compile(r"express|Router|Hono|fastify|Fastify|Koa|polka|Elysia|\.create\(|\.extend\(")
ROUTE_HINT = re.compile(r"\.(?:get|post|put|patch|delete|all|head|options)\s*\(\s*['\"`]/")
PATHY = re.compile(r"""(['"])(/[^'"\\\n]{1,200}?)\1|`([^`\\]{0,200}?\$\{[^`]{0,200}?)`""")
CHAIN = re.compile(r"\s*\.\s*(get|post|put|patch|delete|all)\s*\(")
NEXT_METHODS = re.compile(r"^\s*export\s+(?:async\s+)?(?:function|const)\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|ALL|fallback)\b", re.M)
FETCH = re.compile(r"""\b(fetch|axios(?:\.(get|post|put|patch|delete))?|\$\.(?:ajax|get|post)|ky(?:\.(get|post|put|patch|delete))?|\$fetch|ofetch|useFetch)\s*\(\s*""" + JSLIT)
APIFETCH = re.compile(r"""\bapiFetch\s*\(\s*\{([^{}]|\{[^{}]*\})*?\}\s*\)""", re.S)
CLIENT_CALL = re.compile(r"\b(?:fetch|axios(?:\.(?:get|post|put|patch|delete|request))?|ky(?:\.(?:get|post|put|patch|delete))?|\$fetch|ofetch)\s*\(\s*")
JS_FUNC_HEAD = re.compile(r"(?:\bfunction\s+(" + ID + r")\s*\(\s*(" + ID + r")|\b(?:const|let|var)\s+(" + ID + r")\s*=\s*(?:async\s*)?(?:\(\s*(" + ID + r")[^)]*\)|(" + ID + r"))\s*=>)")
FIREBASE_FN = re.compile(r"(?:export\s+const\s+(\w+)|exports\.(\w+))\s*=\s*[\w.()'\"\s,{}:-]{0,160}?\b(onCall|onRequest)\s*(?:<[^>]*>)?\(")
CALLABLE = re.compile(r"\bhttpsCallable(?:FromURL)?\s*(?:<[^>]*>)?\(\s*[\w.]+\s*,\s*(?:['\"]([\w-]+)['\"]|(" + ID + r")\s*[,)])")
ROUTE_TABLE = re.compile(r"""['"](GET|POST|PUT|PATCH|DELETE)\s+(/[^'"\s]*)['"]\s*:\s*([\w$.]+)?""")
METHOD = re.compile(r"""\bmethod\s*:\s*['"](\w+)['"]""", re.I)
URL = re.compile(r"""https?://([a-z0-9][a-z0-9.-]*\.[a-z]{2,})(?::\d+)?[/'"`\s?]""", re.I)

PHP_CLASS = re.compile(r"^\s*(?:(?:final|abstract|readonly)\s+)*(class|interface|trait|enum)\s+(\w+)(?:\s+extends\s+\\?([\w\\]+))?", re.M)
PHP_FUNC = re.compile(r"^\s*(?:(?:public|protected|private|static|final|abstract)\s+)*function\s+&?(\w+)", re.M)
PHP_NS = re.compile(r"^\s*namespace\s+([\w\\]+)\s*;", re.M)
PHP_REF = re.compile(r"""(?:\bnew\s+\\?([\w\\]+)|\b([A-Z][\w\\]*)::(?!class\b)|\b(?:extends|implements|instanceof)\s+\\?([\w\\]+)|^\s*use\s+\\?([\w\\]+)(?:\s+as\s+\w+)?\s*;)""", re.M)
PHP_INCLUDE = re.compile(r"""\b(?:require|include)(?:_once)?\b\s*\(?([^;]+);""")
PHP_CONST = re.compile(r"""\bconst\s+(\w+)\s*=\s*""" + STR)
PHP_DEFINE = re.compile(r"""\bdefine\s*\(\s*['"](\w+)['"]\s*,\s*""" + STR)
WP_ROUTE = re.compile(r"""\bregister_rest_route\s*\(""")
WP_HOOK = re.compile(r"""\badd_action\s*\(\s*([^,]*?(?:wp_ajax_|admin_post_)[^,]*?)\s*,\s*""")
WP_MENU = re.compile(r"""\badd_(menu|submenu|options|management|theme|users|dashboard|posts|media|pages|comments|plugins|utility|object)_page\s*\(""")
WP_SHORTCODE = re.compile(r"""\badd_shortcode\s*\(""")
WP_BLOCK = re.compile(r"""\bregister_block_type(?:_from_metadata)?\s*\(""")
LARAVEL = re.compile(r"""\bRoute::(get|post|put|patch|delete|any|match|resource|apiResource)\s*\(""")
WP_METHODS = {"READABLE": ["GET"], "CREATABLE": ["POST"], "EDITABLE": ["POST", "PUT", "PATCH"], "DELETABLE": ["DELETE"], "ALLMETHODS": ["ANY"]}

PY_IMPORT = re.compile(r"^\s*(?:from\s+(\.*[\w.]*)\s+import\s+([\w*, ()]+)|import\s+([\w., ]+))", re.M)
PY_DEF = re.compile(r"^(?:async\s+)?(?:def|class)\s+(\w+)", re.M)
PY_ROUTE = re.compile(r"""@(\w+)\.(route|get|post|put|patch|delete|api_route|websocket)\s*\(\s*""" + STR + r"""([^)]*)""")
PY_APP = re.compile(r"^(\w+)\s*=\s*(Flask|FastAPI|Starlette|Quart|Sanic|Blueprint|APIRouter)\s*\(", re.M)
PY_MOUNT = re.compile(r"\b(\w+)\.(register_blueprint|include_router)\s*\(")
PY_REQ = re.compile(r"""\b(?:requests|httpx|session|client)\.(get|post|put|patch|delete)\s*\(\s*f?""" + STR)
DJANGO = re.compile(r"""\b(?:re_)?path\s*\(""")
GO_ROUTE = re.compile(r"\b(\w+)\.(HandleFunc|Handle|GET|POST|PUT|PATCH|DELETE|Any|Get|Post|Put|Patch|Delete|Head|Options)\s*\(\s*\"")
GO_GROUP = re.compile(r"\b(\w+)\s*:?=\s*(\w+)\.Group\(\s*\"([^\"]*)\"")
SPRING = re.compile(r"@(Get|Post|Put|Patch|Delete|Request)Mapping\b(?:\s*\(([^)]*)\))?")
ACTIX = re.compile(r"#\[(get|post|put|patch|delete|head)\(\s*\"([^\"]*)\"")

# Who may call an endpoint: names of middleware/decorators that check the user, and checks inside a handler.
ADMIN_NAMES = re.compile(r"admin|superuser|is_?staff|require_?role|has_?role|check_?role|manage_options", re.I)
AUTH_NAMES = re.compile(r"auth|require_?user|logged_?in|login_?required|(?<![a-z])protect|verify_?(?:token|jwt|session|user)|passport|"
                        r"jwt|guard|current_?user|require_?login|require_?session|signed_?in|bearer|ensure_?user", re.I)
SESSION = re.compile(r"getServerSession|\bauth\(\)|currentUser\(|getAuth\(|auth\.getUser|\.getUser\(|getSession\(|requireUser|requireAuth|"
                     r"withApiAuthRequired|getToken\(|verifyIdToken|\breq(?:uest)?\.user\b|\b(?:request|context)\.auth\b|isAuthenticated|"
                     r"jwt\.verify|verifyToken|locals\.(?:user|session)|event\.context\.user|UserSession\(|current_user\b|"
                     r"verify_jwt_in_request|login_required|is_user_logged_in|current_user_can")
HANDLER_HINT = re.compile(r"getServerSession|\bauth\(\)|currentUser\(|getAuth\(|auth\.getUser|\.getUser\(|getSession\(|withApiAuthRequired|getToken\(|"
                          r"verifyIdToken|\breq(?:uest)?\.user\b|\b(?:request|context)\.auth\b|jwt\.verify|locals\.(?:user|session)|event\.context\.user|UserSession\(")
ADMIN_BODY = re.compile(r"isAdmin|is_admin|role\W{1,8}admin|is_superuser|is_staff|token\.admin|manage_options")
NONCE = re.compile(r"nonce|csrf|xsrf|check_admin_referer|check_ajax_referer|signature|hmac|captcha|turnstile|constructEvent|verifyWebhook", re.I)
ADMIN_CAPS = re.compile(r"manage_options|administrator|activate_plugins|install_plugins|edit_theme_options|manage_network|delete_users|"
                        r"create_users|edit_users|promote_users|manage_woocommerce|update_core|edit_plugins|unfiltered_html")


def lineno(text, pos):
    return text.count("\n", 0, pos) + 1


def lit(m, start=0, end=None):
    """The first string-literal group that matched, from groups[start:end]."""
    return next((g for g in m.groups()[start:end] if g is not None), "")


def strval(s):
    """The value of a lone string literal ('x', "x", `x`), else None."""
    s = s.strip()
    if len(s) >= 2 and s[0] in "'\"`" and s[-1] == s[0]:
        return s[1:-1]
    return None


def php_expr(expr, consts, text="", depth=0):
    """Evaluate a PHP string expression like `'/' . self::BASE . '/(?P<id>\\d+)'` as far as it can."""
    sp = re.match(r"\s*sprintf\s*\(", expr)
    if sp and depth < 3:  # sprintf( '%s/%s/%d/start', A::NS, B::BASE, $id )
        a = split_top(call_args(expr, sp.end()))
        vals = iter([php_expr(x, consts, text, depth + 1) for x in a[1:]])
        return re.sub(r"%[sdu]", lambda _: next(vals, "*"), php_expr(a[0], consts, text, depth + 1)) if a else "*"
    out = []
    for part in re.split(r"\s*\.\s*(?=(?:[^'\"]|'[^']*'|\"[^\"]*\")*$)", expr.strip()):
        m = re.fullmatch(r"\s*" + STR + r"\s*", part)
        if m:
            out.append(re.sub(r"\{?\$\w+\}?", "*", lit(m)) if m.group(2) is not None else lit(m))
            continue
        v = re.fullmatch(r"\s*\$(\w+)\s*", part)
        a = v and depth < 3 and re.search(r"\$" + v.group(1) + r"\s*=\s*([^;=][^;]*);", text)
        if a:  # a local variable assigned once above, like $base = '/' . self::BASE;
            out.append(php_expr(a.group(1), consts, text, depth + 1))
            continue
        if re.fullmatch(r"\s*\d+\s*", part):
            out.append(part.strip())
            continue
        c = re.fullmatch(r"\s*(?:\\?(?:[\w]+\\)*(\w+)::)?(\w+)\s*", part)
        out.append(consts.get(f"{c.group(1)}::{c.group(2)}", consts.get(c.group(2), "*")) if c else "*")
    return "".join(out)


def php_label(expr, consts, text=""):
    """A menu title: a literal, a translated literal (__('X')), or a constant."""
    m = re.match(r"\s*(?:\w*__|_x|_e|esc_html_e|esc_attr_e)?\s*\(?\s*" + STR, expr)
    return lit(m) if m else php_expr(expr, consts, text)


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
        elif ch in "'\"`":
            q = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        i += 1
    return text[start:i - 1]


def split_top(s, sep=","):
    """Split call arguments at top-level separators."""
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
        elif ch in "'\"`":
            q = ch
            cur += ch
        elif ch in "([{":
            depth += 1
            cur += ch
        elif ch in ")]}":
            depth -= 1
            cur += ch
        elif ch == sep and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
        i += 1
    if cur.strip():
        parts.append(cur)
    return parts


def eval_js(raw, consts, depth=0):
    def sub(m):
        expr = m.group(1).strip()
        if re.fullmatch(r"[A-Za-z_$][\w$]*", expr) and expr in consts and depth < 4:
            return eval_js(consts[expr], consts, depth + 1)
        return "*"
    return re.sub(r"\$\{([^{}]*)\}", sub, raw)


def eval_expr(expr, consts):
    """A JS string expression ('a' + B + `c${D}`) as text, unknown bits as *."""
    out = []
    for part in split_top(expr, "+"):
        part = part.strip()
        v = strval(part)
        if v is not None:
            out.append(eval_js(v, consts) if part[0] == "`" else v)
        elif part in consts:
            out.append(eval_js(consts[part], consts))
        elif part:
            out.append("*")
    return "".join(out)


def join_path(*bits):
    s = "/".join(b.strip("/") for b in bits if b and b.strip("/"))
    return "/" + s


def names_in(s):
    """The identifiers in middleware/decorator text, without string contents (a path like /auth/login isn't a guard)."""
    s = re.sub(r"passport\.(?:initialize|session)\(\)|\bsession\s*\(", "", re.sub(JSLIT, "", s))
    return " ".join(re.findall(r"[A-Za-z_$][\w$.]*", s))


def classify(names):
    if not names:
        return ""
    if ADMIN_NAMES.search(names):
        return "admin"
    return "signed-in" if AUTH_NAMES.search(names) else ""


GUARD_CALL = re.compile(r"\b(?:require|ensure|assert|check|verify|must)_?(?:\w*?)(admin|role|staff|user|auth|session|login|signed_?in)\w*\s*\(", re.I)


def body_auth(body):
    """Auth from the code inside a handler: admin, signed-in, or ''. Own guard helpers count: requireAdmin(), ensureUser()."""
    g = GUARD_CALL.search(body)
    if not SESSION.search(body) and not g:
        return ""
    return "admin" if ADMIN_BODY.search(body) or g and g.group(1).lower() in ("admin", "role", "staff") else "signed-in"


def file_route(rel, index=("index",)):
    """A file-system route ('(shop)/orders/[id]/index') as a path ('/orders/[id]')."""
    out = []
    for s in rel.split("/"):
        if not s or s.startswith("@") or s.startswith("(") and s.endswith(")") or s in index:
            continue
        out.append("*" if s.startswith(("[...", "[[...")) else s)
    return "/" + "/".join(out)


def remix_route(name):
    """A Remix/React Router flat route file name ('api.users.$id', '_auth.login') as a path."""
    out = []
    for s in re.split(r"\.(?![^\[]*\])", name):
        s = s.strip("[]")
        if s in ("_index", "index", "route") or s.startswith("_") or not s:
            continue
        s = s.rstrip("_").strip("()")
        out.append("*" if s == "$" else ":" + s[1:] if s.startswith("$") else s)
    return "/" + "/".join(out)


def js_binds(text):
    """Local names bound by imports: {local: (spec, imported name or 'default' or '*')}."""
    b = {}
    for m in JS_BIND.finditer(text):
        d, named, star, spec = m.groups()
        if d:
            b[d] = (spec, "default")
        if star:
            b[star] = (spec, "*")
        for x in (named or "").split(","):
            a, _, l = x.strip().replace("type ", "").partition(" as ")
            if a.strip():
                b[(l or a).strip()] = (spec, a.strip())
    for m in (REQ_BIND.finditer(text) if "require" in text else ()):
        one, many, spec = m.groups()
        if one:
            b[one] = (spec, "default")
        for x in (many or "").split(","):
            a, _, l = x.strip().partition(":")
            if a.strip():
                b[(l or a).strip()] = (spec, a.strip())
    return b


def spans_at(spans, pos):
    """The in-file prefix and auth of the groups (chi Route, Laravel groups, Actix scopes) around pos."""
    pre, auth = "", ""
    for a, b, p, au in spans:
        if a <= pos < b:
            pre, auth = join_path(pre, p), au or auth
    return pre if pre != "/" else "", auth


def add_ep(f, method, path, line, handler="", auth="", kind="http", **extra):
    f["endpoints"].append({"method": method, "path": path, "line": line, "handler": handler, "auth": auth, "kind": kind, **extra})


def js_endpoints(path, text, f, fw):
    """Express/Koa/Hono/Fastify-style routes, their mounts and guards, file-system routes and Cloud Functions."""
    binds = f["binds"]
    servers, vp, clients = {}, {}, {}
    for m in (JS_DECL.finditer(text) if SERVERISH_TEXT.search(text) else ()):
        rhs = text[m.end():m.end() + 240]
        s = SERVER_RHS.match(rhs)
        if s:
            servers[m.group(1)] = "root" if s.group(1) else "router"
            pm = re.search(r"""prefix\s*:\s*""" + JSLIT, call_args(rhs, rhs.find("(") + 1) if "(" in rhs else "")
            bp = re.match(r"[^;\n]*?\.basePath\(\s*" + JSLIT, rhs)
            pre = strval((pm or bp).group(1)) if pm or bp else None
            if pre:
                vp[m.group(1)] = pre
        elif CLIENT_RHS.match(rhs):
            args = call_args(rhs, rhs.find("(") + 1)
            bm = re.search(r"(?:baseURL|baseUrl|prefixUrl|prefix)\s*:\s*([^,}\n]+)", args)
            clients[m.group(1)] = eval_expr(bm.group(1), f["consts"]).split("*")[0] if bm else ""
    f["wrappers"].update({k: {"kind": "client", "base": v} for k, v in clients.items()})

    def serverish(v):  # an imported `api` or `router` in browser code is a client, not a server
        return v in servers or SERVERY.match(v) and v not in clients and v not in NOT_SERVER and not (v in binds and binds[v][0].startswith((".", "@/", "~/")))

    guards = []  # (var, line, auth)
    for m in (MOUNT.finditer(text) if re.search(r"\.(?:use|route|register)\s*\(", text) else ()):
        var, verb = m.group(1), m.group(2)
        if not serverish(var):
            continue
        args = split_top(call_args(text, m.end()))
        line = lineno(text, m.start())
        prefix, target, mw = None, None, []
        if verb == "use":
            if args and strval(args[0]) is not None:
                prefix, rest = strval(args[0]), args[1:]
            else:
                prefix, rest = "", args
            if not rest:
                continue
            head = re.match(r"\s*(?:await\s+)?(" + ID + r")", rest[-1])
            spec = re.search(r"(?:require|import)\s*\(\s*['\"]([^'\"]+)['\"]", rest[-1])
            is_target = spec or head and (head.group(1) in binds and binds[head.group(1)][0].startswith(".") or servers.get(head.group(1)) == "router")
            if is_target:
                target, mw = rest[-1], rest[:-1]
            else:
                mw = rest
            au = classify(names_in(",".join(rest)))  # app.use('/api', requireAuth): a guard, even when it looks like an imported router
            if au:  # app.use('/api/*', auth) guards everything under /api
                guards.append((var, line, au, re.sub(r"/?\*+$", "", prefix), len(rest) == 1))
            if not target:
                continue
        elif verb == "route":
            if len(args) == 2 and strval(args[0]) is not None:  # Hono app.route('/x', sub)
                prefix, target = strval(args[0]), args[1]
            elif len(args) == 1 and strval(args[0]) is not None:  # Express router.route('/x').get(h).post(h)
                pos = m.end() + len(call_args(text, m.end())) + 1
                while True:
                    c = CHAIN.match(text, pos)
                    if not c:
                        break
                    body = call_args(text, c.end())
                    a = split_top(body)
                    h = a[-1].strip() if a and re.fullmatch(r"[\w$.]+", a[-1].strip()) else ""
                    f["routes"].append({"var": var, "method": c.group(1).upper().replace("ALL", "ANY"), "path": strval(args[0]),
                                        "line": line, "handler": h, "auth": classify(names_in(",".join(a[:-1]))), "body": "" if h else (a[-1] if a else "")})
                    pos = c.end() + len(body) + 1
                continue
            else:
                continue
        else:  # fastify.register(plugin, { prefix: '/v1' })
            if not args:
                continue
            target = args[0]
            pm = re.search(r"prefix\s*:\s*" + JSLIT, args[1]) if len(args) > 1 else None
            prefix = strval(pm.group(1)) if pm else ""
        spec = re.search(r"(?:require|import)\s*\(\s*['\"]([^'\"]+)['\"]", target)
        head = re.match(r"\s*(?:await\s+)?(" + ID + r")", target)
        if not spec and not head:
            continue
        au = classify(names_in(",".join(mw)))
        name = head.group(1) if head else ""
        if not spec and name in servers and name not in binds:  # a router defined in this same file
            vp[name] = join_path(vp.get(var, ""), prefix) if join_path(vp.get(var, ""), prefix) != "/" else ""
            if au:
                guards.append((name, 0, au, "", False))
            continue
        f["mounts"].append({"var": var, "prefix": prefix or "", "spec": spec.group(1) if spec else "", "name": name, "line": line, "auth": au})

    for m in (ROUTE_VERB.finditer(text) if ROUTE_HINT.search(text) else ()):
        var = m.group(1)
        if not serverish(var):
            continue
        args = split_top(call_args(text, text.index("(", m.start() + len(var)) + 1))
        last = args[-1].strip() if len(args) > 1 else ""
        h = last if re.fullmatch(r"[\w$.]+", last) else ""
        f["routes"].append({"var": var, "method": m.group(2).upper().replace("ALL", "ANY"), "path": m.group(4), "line": lineno(text, m.start()),
                            "handler": h, "auth": classify(names_in(",".join(args[1:-1]))), "body": "" if h else last})
    for m in (re.finditer(r"\b(" + ID + r")\s*\.\s*route\s*\(\s*\{", text) if ".route" in text else ()):  # fastify.route({ method, url, handler })
        body = call_args(text, m.end())
        u, me = re.search(r"\b(?:url|path)\s*:\s*" + JSLIT, body), re.search(r"\bmethod\s*:\s*\[?\s*['\"](\w+)", body)
        if u and me and serverish(m.group(1)):
            hm = re.search(r"\bhandler\s*:\s*([\w$.]+)\s*[,}\n]", body)
            f["routes"].append({"var": m.group(1), "method": me.group(1).upper(), "path": strval(u.group(1)), "line": lineno(text, m.start()),
                                "handler": hm.group(1) if hm else "", "auth": classify(names_in(" ".join(re.findall(r"(?:preHandler|onRequest)\s*:\s*([^\n]+)", body)))), "body": body})
    for r in f["routes"]:
        r["vp"] = vp.get(r["var"], "")
        r["root"] = servers.get(r["var"]) == "root"
        g = [a for v, l, a, pre, _ in guards if v == r["var"] and l < r["line"] and (not pre or (r["vp"] + r["path"]).startswith(pre))]
        if not r["auth"] and g:
            r["auth"] = g[-1]
        if not r["auth"] and r.get("body"):
            r["auth"] = body_auth(r["body"])
        r.pop("body", None)
    # A guard registered after every route and mount on its server protects nothing: those routes are open.
    for v, l, a, pre, alone in guards:
        early = [r for r in f["routes"] if r["var"] == v and r["line"] < l and not r["auth"] and (r["vp"] + r["path"]).startswith(pre)]
        later = [x for x in f["routes"] + f["mounts"] if x["var"] == v and x["line"] > l]
        if l and alone and early and not later:
            f["misplaced"].append({"line": l, "routes": [r["line"] for r in early]})
    f["guards"] = [{"var": v, "line": l, "auth": a, "pre": pre} for v, l, a, pre, _ in guards]
    f["servers"] = servers
    for mt in f["mounts"]:
        mt["vp"] = vp.get(mt["var"], "")
        if not mt["auth"]:
            g = [a for v, l, a, pre, _ in guards if v == mt["var"] and l < mt["line"] and (not pre or mt["prefix"].startswith(pre))]
            mt["auth"] = g[-1] if g else ""

    # File-system routes: Next.js, SvelteKit, Nuxt, Astro, Remix/React Router.
    hint = body_auth(text) if re.search(r"(?:^|/)(?:app|pages|routes|server|src/pages)/", path) else ""
    nxt = re.search(r"(?:^|/)app/(.*)/route\.(?:ts|js|mjs)$", path) or re.search(r"(?:^|/)pages/(api/.*?)(?:/index)?\.(?:ts|js|tsx|jsx)$", path)
    svk = re.search(r"(?:^|/)src/routes/(.*?)/?\+server\.(?:ts|js)$", path)
    if nxt or svk:
        route = file_route((nxt or svk).group(1))
        found = list(NEXT_METHODS.finditer(text)) if ("route." in path or svk) else []
        for m in found or ([None] if not svk and "route." not in path else []):
            meth = m.group(1) if m else "ANY"
            add_ep(f, "ANY" if meth in ("ALL", "fallback") else meth, route, lineno(text, m.start()) if m else 1, meth if m else "default", hint or "?file")
    nux = re.search(r"(?:^|/)server/(api|routes)/(.+)\.(?:ts|js|mjs)$", path)
    if nux and re.search(r"\b(?:defineEventHandler|eventHandler)\b", text):
        rel, meth = nux.group(2), "ANY"
        mm = re.search(r"\.(get|post|put|patch|delete)$", rel)
        if mm:
            rel, meth = rel[:mm.start()], mm.group(1).upper()
        add_ep(f, meth, file_route(("api/" if nux.group(1) == "api" else "") + rel), 1, "default", hint or "?file")
    ast = re.search(r"(?:^|/)src/pages/(.+)\.(?:ts|js)$", path)
    if ast and ("astro" in fw or not fw):
        for m in NEXT_METHODS.finditer(text):
            meth = "ANY" if m.group(1) in ("ALL", "fallback") else m.group(1)
            add_ep(f, meth, file_route(ast.group(1)), lineno(text, m.start()), m.group(1), hint or "?file")
    rmx = re.search(r"(?:^|/)app/routes/(.+?)(?:/route)?\.(?:tsx?|jsx?)$", path)
    if rmx and any(d.startswith("@remix-run/") or d in ("@react-router/dev", "react-router") for d in fw):
        for m in re.finditer(r"^\s*export\s+(?:async\s+)?(?:function|const)\s+(loader|action)\b", text, re.M):
            add_ep(f, "GET" if m.group(1) == "loader" else "POST", remix_route(rmx.group(1)), lineno(text, m.start()), m.group(1), hint or "?file")

    # Firebase Cloud Functions: onCall is called by name, onRequest over HTTP.
    for m in (FIREBASE_FN.finditer(text) if "onCall" in text or "onRequest" in text else ()):
        name = m.group(1) or m.group(2)
        body = call_args(text, m.end())
        if m.group(3) == "onCall":
            au = "admin" if re.search(r"\.auth\b[^;]{0,80}(?:token\.admin|role)", body) else "signed-in" if re.search(r"\.auth\b|context\.auth|request\.auth", body) else "public"
            add_ep(f, "CALL", "/" + name, lineno(text, m.start()), name, au, "callable")
        else:
            add_ep(f, "ANY", "/" + name, lineno(text, m.start()), name, body_auth(body) or "public")
    for m in (ROUTE_TABLE.finditer(text) if re.search(r"(?:GET|POST|PUT|PATCH|DELETE) /", text) else ()):
        add_ep(f, m.group(1), m.group(2), lineno(text, m.start()), m.group(3) or "", "unknown")
    if "@Controller" in text:  # NestJS: @Controller('users') + @Get(':id'); guards are often global, so no decorator means unknown
        c = re.search(r"@Controller\(\s*(?:['\"]([^'\"]*)['\"]|\{[^}]*?path\s*:\s*['\"]([^'\"]*)['\"])?", text)
        base = (c.group(1) or c.group(2) or "") if c else ""
        head = text[:c.start()] if c else ""
        cls_auth = "public" if "@Public()" in head[-400:] else classify(names_in(" ".join(re.findall(r"@(?:UseGuards|Roles)\(([^)]*)\)", head[-400:]))))
        for m in re.finditer(r"@(Get|Post|Put|Patch|Delete|All|Head|Options)\(\s*(?:['\"]([^'\"]*)['\"])?\s*\)", text):
            before = text[max(0, m.start() - 300):m.start()]
            deco = before[max(before.rfind(";"), before.rfind("}"), before.rfind("{")) + 1:] + text[m.end():m.end() + 300].split("(")[0]
            h = re.search(r"^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:async\s+)?(\w+)\s*\(", text[m.end():m.end() + 400])
            au = "public" if "@Public()" in deco else classify(names_in(" ".join(re.findall(r"@(?:UseGuards|Roles)\(([^)]*)\)", deco))))
            add_ep(f, m.group(1).upper().replace("ALL", "ANY"), join_path(base, m.group(2) or ""), lineno(text, m.start()), h.group(1) if h else "",
                   au or cls_auth or "unknown")


def js_requests(text, f):
    for m in (FETCH.finditer(text) if re.search(r"fetch|axios|\$\.|\bky\b|useFetch", text) else ()):
        tail = text[m.end():m.end() + 300]
        meth = (m.group(2) or m.group(3) or (METHOD.search(tail).group(1) if METHOD.search(tail) else "")).upper()
        f["requests"].append({"method": meth, "raw": m.group(4)[1:-1], "line": lineno(text, m.start())})
    for m in (APIFETCH.finditer(text) if "apiFetch" in text else ()):
        body = m.group(0)
        pm = re.search(r"""\bpath\s*:\s*(?:\w+\s*\(\s*)?(`(?:[^`\\]|\\.)*`|'[^'\n]*'|"[^"\n]*"|[A-Za-z_$][\w$]*)""", body)
        mm = METHOD.search(body)
        if pm:
            raw = pm.group(1)
            f["requests"].append({"method": (mm.group(1) if mm else "GET").upper(), "raw": raw[1:-1] if raw[0] in "'\"`" else "${" + raw + "}",
                                  "line": lineno(text, m.start())})
    for m in (re.finditer(r"\bfetch\s*\(\s*(?:[\w$]+\.)+(\w+)\s*[,)]", text) if "fetch" in text else ()):  # fetch(boot.restUrl): a URL the server printed into the page
        tail = text[m.end():m.end() + 300]
        f["requests"].append({"method": (METHOD.search(tail).group(1) if METHOD.search(tail) else "").upper(), "raw": "@" + m.group(1), "line": lineno(text, m.start())})
    for m in (CALLABLE.finditer(text) if "httpsCallable" in text else ()):
        if m.group(1):
            f["requests"].append({"method": "CALL", "raw": "/" + m.group(1), "line": lineno(text, m.start())})
    # Local helpers that wrap fetch or httpsCallable: `const call = p => fetch(BASE + p)`.
    for m in (JS_FUNC_HEAD.finditer(text) if re.search(r"fetch\(|httpsCallable|axios|apiFetch|\bky\b", text) else ()):
        name, param = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4) or m.group(5))
        if not param:
            continue
        body = text[m.end():m.end() + 700]
        body = body[:re.search(r"\n(?:export\s|function\s|const\s|let\s|async\s+function\s)|$", body).start()]  # just this function
        c = CALLABLE.search(body)
        if c and c.group(2) == param:
            f["wrappers"][name] = {"kind": "call", "base": ""}
            continue
        c = CLIENT_CALL.search(body)
        a = split_top(call_args(body, c.end())) if c and c.start() < 500 else []
        if not a:  # apiFetch( { path: withQuery( path, query ), method: 'POST' } )
            c = re.search(r"\bapiFetch\s*\(\s*\{", body)
            pm = c and c.start() < 500 and re.search(r"\bpath\s*:\s*([^,}\n]+)", call_args(body, c.end()))
            a = [pm.group(1)] if pm else []
        if a and re.search(r"(?<![\w$.])" + re.escape(param) + r"(?![\w$])", a[0]):
            arg = a[0].strip()
            base = eval_js(arg[1:-1], {**f["consts"], param: ""}) if arg[:1] == "`" else eval_expr(arg, {**f["consts"], param: ""})
            mm = METHOD.search(body[:c.start() + 400])
            verb = re.match(r"(get|post|put|patch|delete|remove|del|destroy)", name, re.I)
            meth = mm.group(1).upper() if mm else {"remove": "DELETE", "del": "DELETE", "destroy": "DELETE"}.get(verb.group(1).lower(), verb.group(1).upper()) if verb else ""
            f["wrappers"][name] = {"kind": "http", "base": base.split("*")[0].rstrip("/"), "method": meth}


def extract(path, text, fw=frozenset()):
    """What one file defines, uses, serves and calls."""
    e = ext(path)
    f = {"lines": text.count("\n") + 1, "defs": [], "imports": [], "refs": [], "pkgs": [], "endpoints": [], "requests": [],
         "strings": [], "hosts": [], "consts": {}, "routes": [], "mounts": [], "guards": [], "misplaced": [], "binds": {},
         "wrappers": {}, "screens": [], "spans": []}
    test = bool(TEST.search(path))
    if e in JS:
        for m in JS_IMPORT.finditer(text):
            spec = m.group(1)
            (f["imports"] if spec.startswith((".", "/", "@/", "~/")) else f["pkgs"]).append(spec)
        f["binds"] = js_binds(text)
        f["defs"] = list(dict.fromkeys(JS_DEF.findall(text) + JS_FN.findall(text)))
        d = JS_DEFAULT.search(text)
        f["default"] = (d.group(1) or d.group(2)) if d else ""
        for m in JS_CONST.finditer(text):
            f["consts"][m.group(1)] = m.group(2)[1:-1]
        if not test:
            js_endpoints(path, text, f, fw)
        js_requests(text, f)
        for m in PATHY.finditer(text):  # path-like strings: '/api/x/y', `${BASE}/orders/${id}`
            s = m.group(2) if m.group(2) is not None else m.group(3)
            if s.startswith("/") and s.count("/") >= 2 and len(s) < 200 and " " not in s or "${" in s and "/" in s and len(s) < 200:
                f["strings"].append(s)
        f["refs"] = sorted(set(re.findall(r"\b([A-Z][A-Z0-9_]{3,})\b", text)))  # constant names: paths built from shared constants
    elif e == ".php" or e == ".blade.php":
        ns = PHP_NS.search(text)
        f["ns"] = ns.group(1) if ns else ""
        f["defs"] = [m.group(2) for m in PHP_CLASS.finditer(text)] + list(dict.fromkeys(PHP_FUNC.findall(text)))[:40]
        f["classes"] = [m.group(2) for m in PHP_CLASS.finditer(text)]
        f["extends"] = [m.group(3).split("\\")[-1] for m in PHP_CLASS.finditer(text) if m.group(3)]
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
        for m in WP_HOOK.finditer(text):
            cb = split_top(call_args(text, text.index("(", m.start()) + 1))
            f["endpoints"].append({"wphook": m.group(1), "cb": cb[1] if len(cb) > 1 else "", "line": lineno(text, m.start())})
        for m in WP_MENU.finditer(text):
            f["screens"].append({"wpmenu": m.group(1), "args": split_top(call_args(text, m.end())), "line": lineno(text, m.start())})
        for m in WP_SHORTCODE.finditer(text):
            f["screens"].append({"wpshort": split_top(call_args(text, m.end()))[:1], "line": lineno(text, m.start())})
        for m in WP_BLOCK.finditer(text):
            f["screens"].append({"wpblock": split_top(call_args(text, m.end()))[:1], "line": lineno(text, m.start())})
        php_laravel(path, text, f)
        for m in re.finditer(r"\bwp_(?:safe_)?remote_(get|post|request)\s*\(\s*" + STR, text):
            f["requests"].append({"method": m.group(1).upper().replace("REQUEST", ""), "raw": lit(m, 1), "line": lineno(text, m.start())})
    elif e == ".py":
        binds = {}
        for m in PY_IMPORT.finditer(text):
            if m.group(1) is not None:
                mod = m.group(1)
                f["imports"].append(mod if mod.strip(".") else mod + "|" + m.group(2))  # `from . import x` names modules
                for x in m.group(2).strip("() ").split(","):
                    a, _, l = x.strip().partition(" as ")
                    if a.strip() and a.strip() != "*":
                        sub = (mod if mod.endswith(".") else mod + ".") + a.strip()
                        binds[(l or a).strip()] = [sub, mod if mod.strip(".") else mod + "|" + a.strip()]
            else:
                for x in m.group(3).split(","):
                    a, _, l = x.strip().partition(" as ")
                    if a.strip():
                        f["imports"].append(a.strip())
                        binds[(l or a.split(".")[0]).strip()] = [a.strip() if l else a.split(".")[0].strip()]
        f["binds"] = binds
        f["defs"] = PY_DEF.findall(text)
        if not test:
            py_endpoints(path, text, f)
        for m in PY_REQ.finditer(text):
            f["requests"].append({"method": m.group(1).upper(), "raw": lit(m, 1), "line": lineno(text, m.start())})
    elif e == ".go":
        f["defs"] = re.findall(r"^func\s+(?:\([^)]*\)\s*)?(\w+)", text, re.M)
        if not test:
            go_endpoints(text, f)
    elif e in CODE:
        f["defs"] = re.findall(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:def|class|fn|struct|func|interface|module)\s+(\w+)", text, re.M)[:40]
        if not test and e == ".rs":
            rust_endpoints(text, f)
        elif not test and e in (".java", ".kt"):
            spring_endpoints(text, f)
    if e in CODE:
        f["hosts"] = sorted({h.lower() for h in URL.findall(text)} - {"localhost", "127.0.0.1", "example.com", "www.w3.org", "schema.org", "github.com", "www.example.com"})
    return f


def php_laravel(path, text, f):
    if "Route::" not in text:
        return
    for m in re.finditer(r"Route::(?:(prefix|middleware)\s*\(([^)]*)\)(?:\s*->\s*\w+\s*\([^)]*\))*|group\s*\(\s*(\[[^\]]*\]))\s*->?\s*(?:group\s*\()?\s*(?:static\s+)?function", text):
        start = text.find("{", m.end())
        if start < 0:
            continue
        body = call_args(text, start + 1)
        chain = m.group(0)
        pm = re.search(r"prefix['\"]?\s*(?:=>|\()\s*['\"]([^'\"]*)", chain)
        f["spans"].append((start, start + len(body), pm.group(1) if pm else "", classify(names_in(" ".join(re.findall(r"['\"]([\w:.-]+)['\"]", chain.split("middleware", 1)[1]))) if "middleware" in chain else "")))
    base = "/api" if re.search(r"(?:^|/)routes/api\.php$", path) else ""
    for m in LARAVEL.finditer(text):
        args = split_top(call_args(text, m.end()))
        verb = m.group(1)
        if verb == "match" and args:
            meths = [x.upper() for x in re.findall(r"['\"](\w+)['\"]", args[0])]
            args = args[1:]
        else:
            meths = [verb.upper().replace("ANY", "ANY")]
        if not args or strval(args[0]) is None:
            continue
        pre, sau = spans_at(f["spans"], m.start())
        route = join_path(base, pre, strval(args[0]))
        tail = text[m.end():text.find(";", m.end())]
        au = classify(names_in(" ".join(re.findall(r"['\"]([\w:.-]+)['\"]", tail.split("middleware", 1)[1])))) if "->middleware" in tail else ""
        h = ""
        if len(args) > 1:
            hm = re.search(r"([\w\\]+)::class\s*,\s*['\"](\w+)", args[1]) or re.search(r"['\"]([\w\\]+@\w+)['\"]", args[1])
            h = (hm.group(1).split("\\")[-1] + ("@" + hm.group(2) if hm.lastindex > 1 else "")) if hm else ""
        if verb in ("resource", "apiResource"):
            item = route + "/{id}"
            rows = [("GET", route, "index"), ("POST", route, "store"), ("GET", item, "show"), ("PUT", item, "update"), ("PATCH", item, "update"), ("DELETE", item, "destroy")]
            if verb == "resource":
                rows += [("GET", route + "/create", "create"), ("GET", item + "/edit", "edit")]
            for meth, p, act in rows:
                add_ep(f, meth, p, lineno(text, m.start()), (h.split("@")[0] + "@" + act) if h else act, au or sau or "unknown")
            continue
        for meth in meths:
            add_ep(f, meth, route, lineno(text, m.start()), h, au or sau or "unknown")


def py_endpoints(path, text, f):
    servers, vp, guards = {}, {}, {}
    for m in PY_APP.finditer(text):
        servers[m.group(1)] = "router" if m.group(2) in ("Blueprint", "APIRouter") else "root"
        args = call_args(text, m.end())
        pm = re.search(r"(?:url_)?prefix\s*=\s*" + STR, args)
        if pm:
            vp[m.group(1)] = lit(pm, 0)
        dm = re.search(r"dependencies\s*=\s*\[([^\]]*)\]", args)
        if dm and classify(names_in(" ".join(re.findall(r"Depends\(\s*([\w.]+)", dm.group(1))))):
            guards[m.group(1)] = classify(names_in(" ".join(re.findall(r"Depends\(\s*([\w.]+)", dm.group(1)))))
    for m in re.finditer(r"@(\w+)\.before(?:_app)?_request\b[^\n]*\n(?:\s*@[^\n]*\n)*\s*def\s+\w+\([^)]*\)\s*:", text):
        au = body_auth(text[m.end():m.end() + 600])
        if au:
            guards[m.group(1)] = au
    f["guards"] = [{"var": k, "line": 0, "auth": v} for k, v in guards.items()]
    for m in PY_ROUTE.finditer(text):
        var, verb = m.group(1), m.group(2).upper()
        if verb == "WEBSOCKET":
            continue
        meths = re.findall(r"['\"](GET|POST|PUT|PATCH|DELETE)['\"]", m.group(5) or "") if verb in ("ROUTE", "API_ROUTE") else [verb]
        d = re.compile(r"(?:async\s+)?def\s+(\w+)\s*\(").search(text, m.end())
        between = text[m.end():d.start()] if d else ""
        sig = call_args(text, d.end()) if d else ""
        decos = [x for x in re.findall(r"^\s*@([\w.]+)", between, re.M) if not re.match(r"\w+\.(route|get|post|put|patch|delete|api_route)$", x)]
        au = classify(names_in(" ".join(decos + re.findall(r"Depends\(\s*([\w.]+)", sig) + re.findall(r"Security\(\s*([\w.]+)", sig))))
        for meth in meths or ["GET"]:
            f["routes"].append({"var": var, "method": meth, "path": lit(m, 2, 4), "line": lineno(text, m.start()), "handler": d.group(1) if d else "",
                                "auth": au or guards.get(var, ""), "vp": vp.get(var, ""), "root": servers.get(var) == "root", "py": True})
    for m in re.finditer(r"\b(\w+)\.add_url_rule\s*\(\s*" + STR, text):
        f["routes"].append({"var": m.group(1), "method": "ANY", "path": lit(m, 1), "line": lineno(text, m.start()), "handler": "", "auth": guards.get(m.group(1), ""),
                            "vp": vp.get(m.group(1), ""), "root": servers.get(m.group(1)) == "root", "py": True})
    for m in PY_MOUNT.finditer(text):
        args = split_top(call_args(text, m.end()))
        if not args:
            continue
        pm = re.search(r"(?:url_)?prefix\s*=\s*" + STR, ",".join(args[1:]))
        au = classify(names_in(" ".join(re.findall(r"Depends\(\s*([\w.]+)", ",".join(args[1:])))))
        f["mounts"].append({"var": m.group(1), "prefix": lit(pm, 0) if pm else "", "spec": "", "name": args[0].strip().split(".")[0],
                            "line": lineno(text, m.start()), "auth": au or guards.get(m.group(1), ""), "replace": m.group(2) == "register_blueprint" and bool(pm),
                            "vp": vp.get(m.group(1), "")})
    f["servers"] = servers
    if path.endswith("urls.py"):
        for m in DJANGO.finditer(text):
            args = split_top(call_args(text, m.end()))
            route = strval(args[0].lstrip("r")) if args else None
            if route is None or len(args) < 2:
                continue
            route = route.lstrip("^").rstrip("$")
            target = args[1].strip()
            if target.startswith("include"):
                inner = call_args(target, target.find("(") + 1)
                sv = strval(split_top(inner)[0]) if inner else None
                f["mounts"].append({"var": "", "prefix": route, "spec": sv or "", "name": "" if sv else inner.split(".")[0].strip(), "line": lineno(text, m.start()),
                                    "auth": "", "py": True, "vp": ""})
            elif "admin.site.urls" not in target:
                h = re.match(r"[\w.]+", target)
                f["routes"].append({"var": "", "method": "ANY", "path": route, "line": lineno(text, m.start()), "handler": h.group(0).split(".")[-1] if h else "",
                                    "auth": "unknown", "vp": "", "root": True, "py": True, "django": True})
    for m in ROUTE_TABLE.finditer(text):
        add_ep(f, m.group(1), m.group(2), lineno(text, m.start()), m.group(3) or "", "unknown")


def go_endpoints(text, f):
    vp, guards = {}, {}
    for m in GO_GROUP.finditer(text):
        vp[m.group(1)] = join_path(vp.get(m.group(2), ""), m.group(3))
        rest = split_top(call_args(text, text.index("(", m.start()) + 1))[1:]
        if classify(names_in(",".join(rest))):
            guards[m.group(1)] = classify(names_in(",".join(rest)))
    for m in re.finditer(r"\b(\w+)\.Use\s*\(", text):
        au = classify(names_in(call_args(text, m.end())))
        if au:
            guards[m.group(1)] = au
    for m in re.finditer(r"\.Route\(\s*\"([^\"]*)\"\s*,\s*func", text):
        f["spans"].append((m.end(), m.end() + len(call_args(text, m.end())), m.group(1), ""))
    for m in re.finditer(r"\b(\w+)\.Mount\(\s*\"([^\"]*)\"\s*,\s*(\w+)", text):
        f["mounts"].append({"var": m.group(1), "prefix": m.group(2), "spec": "", "name": m.group(3), "line": lineno(text, m.start()), "auth": guards.get(m.group(1), ""), "vp": vp.get(m.group(1), "")})
    for m in GO_ROUTE.finditer(text):
        args = split_top(call_args(text, m.end() - 1))
        p = strval(args[0]) if args else None
        if p is None:
            continue
        meth = m.group(2).upper()
        if meth.startswith("HANDLE"):
            pm = re.match(r"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(.*)", p)
            meth, p = (pm.group(1), pm.group(2)) if pm else ("ANY", p)
        if not p.startswith("/"):
            continue
        pre, sau = spans_at(f["spans"], m.start())
        h = re.match(r"\s*([\w.]+)\s*$", args[-1]) if len(args) > 1 else None
        au = (classify(names_in(",".join(args[1:-1]))) or classify(names_in(args[-1]))) if len(args) > 1 else ""
        add_ep(f, meth, join_path(pre, vp.get(m.group(1), ""), p), lineno(text, m.start()),
               h.group(1) if h else "", au or guards.get(m.group(1), "") or sau or "unknown")


def rust_endpoints(text, f):
    for m in re.finditer(r"web::scope\(\s*\"([^\"]*)\"\s*\)", text):
        f["spans"].append((m.end(), m.end() + len(call_args(text, m.end())), m.group(1), ""))
    for m in ACTIX.finditer(text):
        h = re.compile(r"\bfn\s+(\w+)").search(text, m.end())
        add_ep(f, m.group(1).upper(), m.group(2), lineno(text, m.start()), h.group(1) if h else "", "unknown")
    for m in re.finditer(r"web::resource\(\s*\"([^\"]*)\"\s*\)", text):
        end = text.find("web::resource", m.end())
        chunk = text[m.end():end if end > 0 else m.end() + 600][:600]
        pre, _ = spans_at(f["spans"], m.start())
        for r in re.finditer(r"web::(get|post|put|patch|delete)\(\)\s*\.to\(\s*([\w:]+)", chunk):
            add_ep(f, r.group(1).upper(), join_path(pre, m.group(1)), lineno(text, m.start()), r.group(2).split("::")[-1], "unknown")
    for m in re.finditer(r"\.route\(\s*\"([^\"]*)\"\s*,", text):
        args = call_args(text, m.start() + len(".route("))
        pre, _ = spans_at(f["spans"], m.start())
        for r in re.finditer(r"\b(get|post|put|patch|delete|any)\(\s*([\w:]+)", args):
            add_ep(f, r.group(1).upper(), join_path(pre, m.group(1)), lineno(text, m.start()), r.group(2).split("::")[-1], "unknown")
    for m in re.finditer(r"\.nest\(\s*\"([^\"]*)\"\s*,\s*(\w+)", text):
        f["mounts"].append({"var": "", "prefix": m.group(1), "spec": "", "name": m.group(2), "line": lineno(text, m.start()), "auth": "", "vp": ""})


def spring_endpoints(text, f):
    cls = ""
    for m in SPRING.finditer(text):
        after = text[m.end():m.end() + 400]
        args = m.group(2) or ""
        pv = re.search(r"(?:value|path)?\s*=?\s*\{?\s*\"([^\"]*)\"", args)
        p = pv.group(1) if pv else ""
        nxt = re.search(r"\bclass\b|\bfun\b|\(|\{", re.sub(r"@\w+(?:\([^)]*\))?", "", after))
        if m.group(1) == "Request" and nxt and nxt.group(0) == "class":
            cls = p
            continue
        meths = [x for x in re.findall(r"RequestMethod\.(\w+)", args)] if m.group(1) == "Request" else [m.group(1).upper()]
        h = re.search(r"(?:fun\s+|\s)(\w+)\s*\(", re.sub(r"@\w+(?:\([^)]*\))?", "", after))
        before = text[max(0, m.start() - 300):m.start()]
        annot = before[max(before.rfind(";"), before.rfind("}"), before.rfind("{")) + 1:]  # this method's annotations only
        sec = re.findall(r"@(?:PreAuthorize|Secured|RolesAllowed)\(([^)]*)\)", annot)
        au = ("admin" if re.search(r"ADMIN|admin", " ".join(sec)) else "signed-in") if sec else "unknown"
        for meth in meths or ["ANY"]:
            add_ep(f, meth, join_path(cls, p), lineno(text, m.start()), h.group(1) if h else "", au)


# ---------------------------------------------------------------- resolution

def resolve_js(src, spec, files, aliases, paths=()):
    """A JS/TS import as a file: relative, @/ and ~/, tsconfig paths and workspace packages ((prefix, [dirs]) in `paths`)."""
    if spec.startswith(("@/", "~/")) and not any(spec.startswith(a) for a, _ in paths):
        cands = [a + spec[2:] for a in aliases]
    elif spec.startswith("/"):
        cands = [spec.lstrip("/")]
    elif spec.startswith("."):
        cands = [os.path.normpath(os.path.join(os.path.dirname(src), spec))]
    else:
        cands = [os.path.normpath(d + "/" + spec[len(a):].lstrip("/")).rstrip("/.") for a, ds in paths
                 if not a or spec == a or spec.startswith(a.rstrip("/") + "/") for d in ds]
    for c in cands:
        c = c.split("?")[0]
        stem = re.sub(r"\.(?:m|c)?jsx?$", "", c)  # ESM TypeScript imports './x.js' for x.ts
        for x in [c] + [c + e for e in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".scss", ".css")] + \
                 [stem + e for e in (".ts", ".tsx", ".mts", ".cts")] + [c + "/index" + e for e in (".js", ".jsx", ".ts", ".tsx", ".mjs")] + \
                 [c + "/src/index" + e for e in (".ts", ".tsx", ".js")]:
            if x in files:
                return x
    return None


def resolve_php(src, spec, files, tails=None):
    s = spec[2:] if spec.startswith("./") else spec
    for c in (os.path.normpath(os.path.join(os.path.dirname(src), s)), os.path.normpath(s.lstrip("/"))):
        if c in files:
            return c
    tail = (tails or {}).get(os.path.basename(s)) if tails is not None else [p for p in files if p.endswith("/" + s.lstrip("/"))]
    tail = [p for p in tail or [] if p.endswith("/" + s.lstrip("/"))]
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
    p = re.sub(r"\(\?P<\w+>[^)]*\)|\{[^}]*\}|<[^>]+>|:\w+|\[[^\]]+\]|\$\w+", "*", re.split(r"(?<!\()\?", path)[0].split("#")[0])  # a query string, not a (?P<id>) group
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
            best = max(best, lits + (1 if len(sub) == len(ep) else 0) + (1 if k == 0 else 0))
    return best


# ---------------------------------------------------------------- scan

_JOB = {}


class Hit:
    """A picklable stand-in for re.Match (results coming back from worker processes)."""
    def __init__(self, m):
        self.s, self.e, self.g0, self.g = m.start(), m.end(), m.group(0), m.groups()

    def start(self):
        return self.s

    def end(self):
        return self.e

    def groups(self):
        return self.g

    def group(self, i=0):
        return self.g0 if i == 0 else self.g[i - 1]


def _extract_chunk(paths):
    return [(p, extract(p, _JOB["texts"][p], _JOB["fw"](p))) for p in paths]


def _grep_chunk(paths):
    rx = _JOB["rx"]
    return [(p, Hit(m)) for p in paths for m in rx.finditer(_JOB["texts"][p])]


def pool_map(fn, items, **job):
    """fn over chunks of items, spread over the CPU cores for big projects (forked workers share `job`); serial otherwise."""
    import multiprocessing
    import threading
    n = min(os.cpu_count() or 1, 8)
    _JOB.update(job)
    try:
        # ponytail: fork only from a single-threaded process (the CLI); the board server's rescans run serially.
        if len(items) > 400 and n > 1 and threading.active_count() == 1 and "fork" in multiprocessing.get_all_start_methods():
            import warnings
            from concurrent.futures import ProcessPoolExecutor
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    with ProcessPoolExecutor(n, mp_context=multiprocessing.get_context("fork")) as ex:
                        return [x for part in ex.map(fn, [items[i::n * 4] for i in range(n * 4)]) for x in part]
            except (OSError, RuntimeError, ValueError, ImportError):
                pass
        return fn(items)
    finally:
        _JOB.clear()


def extract_all(paths, texts, fw_of):
    return dict(pool_map(_extract_chunk, paths, texts=texts, fw=fw_of))


FRAMEWORKS = ("next", "nuxt", "astro", "@sveltejs/kit", "expo-router", "react-router", "react-router-dom", "vue-router", "@angular/router",
              "@tanstack/react-router", "@remix-run/react", "@remix-run/node", "@react-router/dev", "express", "hono", "fastify")


def scan():
    repos = find_repos()
    paths = list_files(repos)
    files, texts, raws = {}, {}, {}
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
        texts[p], raws[p] = text, raw

    # Which frameworks each folder uses (from the nearest package.json and the repo's own).
    deps = {}
    for p, t in texts.items():
        if os.path.basename(p) == "package.json":
            try:
                j = json.loads(t)
                deps[os.path.dirname(p)] = {d for k in ("dependencies", "devDependencies", "peerDependencies") for d in (j.get(k) or {}) if d in FRAMEWORKS or d.startswith("@remix-run/")}
            except (ValueError, AttributeError):
                pass
    fw_cache = {}

    def fw_of(p):
        d = os.path.dirname(p)
        if d not in fw_cache:
            out, x = set(), d
            while True:
                out |= deps.get(x, set())
                if not x:
                    break
                x = os.path.dirname(x)
            fw_cache[d] = frozenset(out)
        return fw_cache[d]

    done = extract_all([p for p in texts if ext(p) in CODE or ext(p) in STYLE], texts, fw_of)
    for p, text in texts.items():
        if p in done:
            f = done[p]
        else:
            f = {"lines": text.count("\n") + 1, "config": True}
        f["hash"] = hashlib.sha1(raws[p]).hexdigest()[:12]
        f["repo"] = p.split("/")[0] if repos and "/" in p and p.split("/")[0] in repos else ""
        files[p] = f
    del raws
    code = {p for p, f in files.items() if not f.get("config")}
    by_base = {}
    for p in files:
        by_base.setdefault(os.path.basename(p), []).append(p)

    # Constants shared across files (JS path constants, PHP class constants).
    js_consts, php_consts = {}, {}
    for p, f in files.items():
        for k, v in (f.get("consts") or {}).items():
            (php_consts if ext(p) == ".php" else js_consts).setdefault(k, v)
            for c in f.get("classes", [])[:1]:  # Rest_Quizzes::REST_BASE, when several classes share a constant name
                php_consts[f"{c}::{k}"] = v

    # Imports and references between files.
    classes = {}
    for p, f in files.items():
        for c in f.get("classes", []):
            classes.setdefault(c, []).append(p)
    repo_pre = {r: r + "/" for r in repos} if repos else {"": ""}
    aliases = {r: [pre + a for a in ("src/", "app/", "") if any(x.startswith(pre + a) for x in code)] for r, pre in repo_pre.items()}
    pypkgs = {r: [pre] + sorted({pre + p[len(pre):].split("/")[0] + "/" for p in code if p.endswith(".py") and p.startswith(pre) and "/" in p[len(pre):]} - {pre + "tests/"})
              for r, pre in repo_pre.items()}

    ws = []  # (import prefix, [folders]): workspace packages by name, tsconfig/jsconfig "paths"
    for p, t in texts.items():
        b, d = os.path.basename(p), os.path.dirname(p)
        if b == "package.json" and d:
            try:
                name = json.loads(t).get("name")
            except (ValueError, AttributeError):
                name = None
            if isinstance(name, str) and name:
                ws.append((name, [d + "/src", d]))
        elif re.fullmatch(r"[tj]sconfig(?:\.[\w-]+)?\.json", b):
            try:
                co = json.loads(re.sub(r",(\s*[}\]])", r"\1", re.sub(r"(?m)^\s*//.*$|/\*.*?\*/", "", t))).get("compilerOptions") or {}
            except (ValueError, AttributeError):
                continue
            base = os.path.normpath(os.path.join(d, co.get("baseUrl") or "."))
            if co.get("baseUrl"):  # bare imports like 'src/modules/x' resolve from baseUrl
                ws.append(("", [base.lstrip("./") if base != "." else ""]))
            for k, v in (co.get("paths") or {}).items():
                if isinstance(v, list):
                    ws.append((k.rstrip("*"), [os.path.normpath(os.path.join(base, x.rstrip("*"))).lstrip("./") if x.rstrip("*") not in ("", "./") else base for x in v if isinstance(x, str)]))
    ws.sort(key=lambda x: -len(x[0]))

    def res(p, spec):
        e, r = ext(p), files[p].get("repo", "")
        if e in JS:
            return [x for x in [resolve_js(p, spec, files, aliases.get(r, [""]), ws)] if x]
        if e == ".py":
            return resolve_py(p, spec, files, pypkgs.get(r, [""]))
        if e == ".php":
            return [x for x in [resolve_php(p, spec, files, by_base)] if x]
        return []

    for p, f in files.items():
        if f.get("config"):
            continue
        uses = set()
        for spec in f.get("imports", []) + ([x for x in f.get("pkgs", []) if ws] if ext(p) in JS else []):
            uses.update(res(p, spec))
        if ext(p) == ".php":
            for c in f.get("refs", []):
                hit = classes.get(c, [])
                if len(hit) == 1:
                    uses.add(hit[0])
        uses.discard(p)
        f["uses"] = sorted(uses)

    def bound_file(p, name):
        """The file a local name in p was imported from."""
        b = (files[p].get("binds") or {}).get(name)
        if not b:
            return None
        if ext(p) == ".py":
            for spec in b:
                r = res(p, spec)
                if r:
                    return r[0]
            return None
        r = res(p, b[0])
        return r[0] if r else None

    defs_at = {}
    for p, f in files.items():
        for d in f.get("defs") or []:
            defs_at.setdefault(d, []).append(p)

    # Mounts: which file's routes sit under which prefix, and the guards they inherit.
    mounted = {}  # file -> [(mounter file, mount)]
    for p, f in files.items():
        for mt in f.get("mounts", []):
            t = (res(p, mt["spec"]) or [None])[0] if mt.get("spec") and ext(p) != ".py" else \
                (resolve_py(p, mt["spec"], files, pypkgs.get(f.get("repo", ""), [""])) or [None])[0] if mt.get("spec") else \
                bound_file(p, mt["name"]) if mt["name"] in (f.get("binds") or {}) else \
                (defs_at.get(mt["name"]) or [None])[0] if len(defs_at.get(mt["name"], [])) == 1 else None
            if t and t != p:
                mounted.setdefault(t, []).append((p, mt))

    def prefix_of(p, seen=()):
        """(prefix, inherited auth, reached from a root server) for a file's routes."""
        if p in seen or p not in mounted:
            return "", "", False
        q, mt = mounted[p][0]
        pre, au, rooted = prefix_of(q, seen + (p,))
        own = [g["auth"] for g in files[q].get("guards", []) if g["var"] == mt["var"] and g["line"] < mt["line"] and mt["prefix"].startswith(g.get("pre", ""))]
        rooted = rooted or (files[q].get("servers") or {}).get(mt["var"]) == "root" or bool(mt.get("py")) or not mt["var"]
        return join_path(pre, mt.get("vp", ""), mt["prefix"]).rstrip("/") if join_path(pre, mt.get("vp", ""), mt["prefix"]) != "/" else "", \
            mt["auth"] or (own[-1] if own else "") or au, rooted

    guard_repos = set()  # repos with a framework-wide auth middleware (Next middleware.ts, SvelteKit hooks, Nuxt server middleware, ...)
    for p, t in texts.items():
        if re.search(r"(?:^|/)(?:src/)?(?:middleware|hooks\.server|proxy)\.(?:ts|js)$|(?:^|/)server/middleware/", p) and (SESSION.search(t) or AUTH_NAMES.search(names_in(t[:4000]))):
            guard_repos.add(files[p].get("repo", ""))
        elif ext(p) == ".py" and re.search(r"before_request|add_middleware\([^)]*Auth|AuthenticationMiddleware", t) and SESSION.search(t):
            guard_repos.add(files[p].get("repo", ""))

    def handler_auth(p, h):
        """Auth checks inside a named handler: same file or the file it's imported from. None = can't see it."""
        if not h:
            return ""
        head, _, attr = h.partition(".")
        q = bound_file(p, head) if head in (files[p].get("binds") or {}) else p
        if not q or q not in texts:
            return None
        name = attr or head
        t = texts[q]
        m = re.search(r"(?:function\s+" + re.escape(name) + r"\b|\b" + re.escape(name) + r"\s*[:=]\s*(?:async\s*)?(?:\(|function|\w+\s*=>)|\bdef\s+" + re.escape(name) + r"\b|\b" + re.escape(name) + r"\s*\()", t)
        if not m:
            return None
        return body_auth(t[m.start():m.start() + 1500])

    # Endpoints with their full paths and who may call them.
    endpoints = {}

    def put(key, ep):
        old = endpoints.get(key)
        if old:  # wp_ajax_x and wp_ajax_nopriv_x are one endpoint; nopriv makes it public
            if ep.get("auth") == "public":
                old["auth"], old["public"] = "public", True
            return
        endpoints[key] = ep

    php_funcs = {}
    for p, f in files.items():
        if ext(p) == ".php":
            for m in re.finditer(r"\bfunction\s+&?(\w+)\s*\(", texts.get(p, "")):
                php_funcs.setdefault(m.group(1), []).append((p, m.start()))

    def php_body(p, name):
        """The body of a PHP function or method: this file, its parent class's file, or the only one with that name."""
        hits = php_funcs.get(name, [])
        mine = [h for h in hits if h[0] == p]
        if not mine:
            parents = {q for c in files[p].get("extends", []) for q in classes.get(c, [])}
            mine = [h for h in hits if h[0] in parents] or (hits if len(hits) == 1 else [])
        if not mine:
            return None
        q, at = mine[0]
        return texts[q][at:at + 1500], q

    def wp_check(p, expr, consts):
        """Auth for a WordPress callback expression (permission_callback or an ajax handler)."""
        if "__return_true" in expr:
            return "public"
        if "__return_false" in expr:
            return "admin"
        found = re.search(r"current_user_can\s*\(\s*([^)]*)\)|is_user_logged_in", expr)
        body, q = expr, p
        if not found:
            names = re.findall(r"['\"](\w+)['\"]", expr)
            got = php_body(p, names[-1]) if names else None
            if not got:
                return "unknown"
            body, q = got
        cap = re.search(r"current_user_can\s*\(\s*([^),]*)", body)
        if cap:
            v = php_expr(cap.group(1), {**php_consts, **(files[q].get("consts") or {})}, texts.get(q, ""))
            return "admin" if ADMIN_CAPS.search(v) else "signed-in"
        if "is_user_logged_in" in body:
            return "signed-in"
        if re.search(r"return\s+true\s*;", body[:400]) and not re.search(r"\bif\b", body[:400]):
            return "public"
        return "unknown"

    for p, f in files.items():
        text = texts.get(p, "")
        consts = {**php_consts, **(f.get("consts") or {})}  # the class's own constants first
        nonce = bool((f.get("endpoints") or f.get("routes")) and NONCE.search(text))
        for ep in f.get("endpoints", []):
            if "wp" in ep:
                ns = php_expr(ep["wp"][0], consts, text).strip("/")
                route = php_expr(ep["wp"][1], consts, text)
                path = "/" + ns + "/" + route.lstrip("/")
                args = ep["args"]
                found = list(re.finditer(r"""['"]methods['"]\s*=>\s*(?:\\?WP_REST_Server::(\w+)|""" + STR + ")", args)) or [None]
                for i, m in enumerate(found):  # each handler array has its own methods and permission_callback
                    seg = args[m.start() if m else 0:found[i + 1].start() if i + 1 < len(found) else len(args)]
                    back = args[found[i - 1].end() if i else 0:m.start()] if m else ""
                    perm = re.search(r"""['"]permission_callback['"]\s*=>\s*([^\n]+)""", seg) or re.search(r"""['"]permission_callback['"]\s*=>\s*([^\n]+)""", back[-600:] if "array" not in back[-600:].split("permission_callback")[-1] else "")
                    if perm and "function" in perm.group(1):  # an inline closure: read its body
                        perm_text = args[args.find(perm.group(0)):][:500]
                    else:
                        perm_text = perm.group(1) if perm else ""
                    cb = re.search(r"""['"]callback['"]\s*=>\s*([^\n]+)""", seg) or re.search(r"""['"]callback['"]\s*=>\s*([^\n]+)""", back[-600:])
                    hn = re.findall(r"['\"](\w+)['\"]", re.split(r"\)|,\s*['\"]\w+['\"]\s*=>", cb.group(1))[0]) if cb else []
                    meths = (WP_METHODS.get(m.group(1), []) if m.group(1) else [x.strip().upper() for x in lit(m, 1).split(",")]) if m else ["GET"]
                    auth = wp_check(p, perm_text, consts) if perm else "public"
                    for meth in meths:
                        put(f"{meth} {path}", {"method": meth, "path": path, "kind": "http", "file": p, "line": ep["line"], "handler": hn[-1] if hn else "",
                                               "auth": auth, "public": auth == "public", "checked": bool(perm), "nonce": nonce})
            elif "wphook" in ep:
                hook = php_expr(ep["wphook"], consts, text)
                hm = re.match(r"(wp_ajax_|admin_post_)(nopriv_)?(.+)$", hook)
                if not hm or "*" in hm.group(3):
                    continue
                hn = re.findall(r"['\"](\w+)['\"]", ep["cb"])
                if hm.group(2):
                    auth = "public"
                else:
                    got = php_body(p, hn[-1]) if hn else None
                    auth = wp_check(p, got[0], consts) if got and "current_user_can" in got[0] else "signed-in"
                ajax = hm.group(1) == "wp_ajax_"
                meth, path = ("AJAX", "/wp-admin/admin-ajax.php?action=" + hm.group(3)) if ajax else ("POST", "/wp-admin/admin-post.php?action=" + hm.group(3))
                put(f"{meth} {path}", {"method": meth, "path": path, "kind": "ajax" if ajax else "http", "file": p, "line": ep["line"],
                                       "handler": hn[-1] if hn else "", "auth": auth, "public": auth == "public", "nonce": nonce})
            else:
                auth = ep.get("auth") or "unknown"
                if auth == "?file":  # file-system route with no check in the file: open unless a framework middleware guards it
                    auth = "unknown" if f.get("repo", "") in guard_repos else "public"
                put(f"{ep['method']} {ep['path']}", {"method": ep["method"], "path": ep["path"], "kind": ep.get("kind", "http"), "file": p, "line": ep["line"],
                                                     "handler": ep.get("handler", ""), "auth": auth, "public": auth == "public", "nonce": nonce})
            ep.pop("args", None)
        pre, inherited, rooted = prefix_of(p)
        for r in f.get("routes", []):
            full = join_path(pre, "" if (mounted.get(p) and mounted[p][0][1].get("replace")) else r["vp"], r["path"])
            auth = r["auth"] or inherited
            if not auth:
                ha = handler_auth(p, r["handler"])
                if ha:
                    auth = ha
                elif ha is None or not (rooted or r["root"]):  # can't see the handler, or nothing we found mounts this router
                    auth = "unknown"
                elif ext(p) == ".py" and f.get("repo", "") in guard_repos or HANDLER_HINT.search(text) and not r.get("py"):  # checks elsewhere in the file
                    auth = "unknown"
                else:
                    auth = "public"
            put(f"{r['method']} {full}", {"method": r["method"], "path": full, "kind": "http", "file": p, "line": r["line"], "handler": r["handler"],
                                          "auth": auth, "public": auth == "public", "nonce": nonce})
        f.pop("endpoints", None)
    ep_segs = {k: segs(v["path"]) for k, v in endpoints.items()}
    by_seg, by_last = {}, {}  # path segment -> endpoints that have it, so each request only scores the endpoints that could fit
    for k, es in ep_segs.items():
        for x in set(es):
            by_seg.setdefault(x, []).append(k)
        by_last.setdefault(es[-1] if es else "*", []).append(k)
    printed = {}  # WordPress: 'restUrl' => rest_url( 'ns/v1/x' ) handed to scripts (wp_localize_script and friends)
    for p, t in texts.items():
        if ext(p) == ".php" and "rest_url" in t:
            consts = {**php_consts, **(files[p].get("consts") or {})}
            for m in re.finditer(r"['\"](\w+)['\"]\s*=>\s*(?:esc_url_raw\s*\(\s*)?(?:get_)?rest_url\s*\(", t):
                a = split_top(call_args(t, m.end()))
                if a:
                    printed.setdefault(m.group(1), set()).add("/" + php_expr(re.sub(r"\(\s*(?:string|int)\s*\)", "", a[0]), consts, t).strip("/"))
    memo = {}

    def matches(meth, u, base):
        """The endpoints a request (method, url) fits best; the same strings recur across files, so it's memoized."""
        key = (meth, u, base)
        if key not in memo:
            cs = segs(u.split("/wp-json", 1)[-1])
            last = cs[-1] if cs else "*"  # the request's last segment lines up with the endpoint's last one
            pool = by_last.get(last, []) + by_last.get("*", []) if "*" not in last else \
                sorted({k for x in cs if "*" not in x for k in by_seg.get(x, ())}) if any("*" not in x for x in cs) else ep_segs
            scored = [(match_score(cs, ep_segs[k]), k) for k in pool
                      if (not meth or endpoints[k]["method"] in (meth, "ANY") or meth == "ANY") and (meth == "CALL") == (endpoints[k]["method"] == "CALL")]
            top = max((s for s, _ in scored), default=0)
            best = [k for s, k in scored if s and s == top]
            if base and len(best) > 1:
                best = [k for k in best if endpoints[k]["path"].startswith(base)] or best
            memo[key] = best
        return memo[key]

    mis = {p: f.pop("misplaced") for p, f in files.items() if f.get("misplaced")}

    # Requests matched to endpoints: explicit calls, wrappers and clients, then path-like strings and shared path constants.
    for p, f in files.items():
        if f.get("config"):
            continue
        local = dict(f.get("wrappers") or {})
        if ext(p) in JS:
            for name, (spec, imported) in (f.get("binds") or {}).items():
                q = bound_file(p, name)
                if q and files[q].get("wrappers"):
                    w = files[q]["wrappers"]
                    key = files[q].get("default") if imported == "default" else imported
                    if key in w:
                        local[name] = w[key]
        bases = []
        if local:
            rx = re.compile(r"(?<![\w$.])(" + "|".join(map(re.escape, local)) + r")(?:\s*\.\s*(get|post|put|patch|delete|request))?\s*(?:<[^>()]*>)?\(\s*" + JSLIT)
            text = texts.get(p, "")
            for m in rx.finditer(text):
                w = local[m.group(1)]
                if w["kind"] == "client" and not m.group(2) or w["kind"] != "client" and m.group(2):
                    continue
                raw = m.group(3)[1:-1]
                if w["kind"] == "call":
                    f["requests"].append({"method": "CALL", "raw": "/" + raw, "line": lineno(text, m.start())})
                    continue
                tail = text[m.end():m.end() + 300]
                meth = (m.group(2) or w.get("method") or (METHOD.search(tail).group(1) if METHOD.search(tail) else "")).upper().replace("REQUEST", "")
                f["requests"].append({"method": meth, "raw": w["base"] + raw if raw.startswith("/") or not raw else w["base"] + "/" + raw, "line": lineno(text, m.start()), "lit": raw})
                if w["base"]:
                    bases.append(w["base"])
        base = max(set(bases), key=bases.count) if bases else ""  # the base path this client file usually calls
        reqs, seen = [], set()
        cs_consts = {**js_consts, **f.get("consts", {})}
        cands = [(r.get("method", ""), eval_js(r["raw"], cs_consts), r["line"]) for r in f.get("requests", []) if r["raw"][:1] != "@"]
        cands += [(r.get("method", ""), u, r["line"]) for r in f.get("requests", []) if r["raw"][:1] == "@" for u in sorted(printed.get(r["raw"][1:], ()))]
        own = set((f.get("consts") or {}).values())  # a file that only defines path constants doesn't call them
        asked = {r.get("lit", r["raw"]) for r in f.get("requests", [])} | {r["raw"] for r in f.get("requests", [])}  # strings already seen as a call with its method
        cands += [("", eval_js(s, cs_consts), 0) for s in f.get("strings", []) if s not in own and s not in asked]
        cands += [("", eval_js(js_consts[r], js_consts), 0) for r in f.get("refs", []) if r in js_consts and "/" in js_consts[r] and r not in (f.get("consts") or {})]
        for meth, url, line in cands:
            if url.startswith("http") and "/wp-json/" not in url:
                continue
            tries = [url] + ([base + url] if base and url.startswith("/") and not url.startswith(base) else [])
            for u in tries:
                best = matches(meth, u, base)
                for k in best:
                    if k not in seen and endpoints[k]["file"] != p:
                        seen.add(k)
                        reqs.append({"to": k, "line": line})
                if best:
                    break
        f["calls"] = reqs
        f["sig"] = hashlib.sha1(json.dumps([f.get("defs"), [r["to"] for r in reqs], f.get("ns", "")], sort_keys=True).encode()).hexdigest()[:12]

    screens = find_screens(files, texts, fw_of, bound_file, res, php_consts)
    unused = find_unused(files, texts, endpoints, screens)
    for p in unused:
        files[p]["unused"] = True
    for f in files.values():
        for k in ("imports", "strings", "requests", "consts", "refs", "classes", "extends", "routes", "mounts", "guards", "binds", "wrappers",
                  "default", "screens", "spans", "servers", "enq"):
            f.pop(k, None)
    return {"scanned": now(), "files": files, "endpoints": endpoints, "screens": screens, "repos": repos, "misplaced": mis, "_texts": texts}


# ---------------------------------------------------------------- screens

def route_name(route):
    bits = [b for b in route.strip("/").split("/") if b and not re.match(r"^[\[:$*<{(]", b)]
    if not bits:
        return "Home" if not route.strip("/") else route.strip("/")
    return " ".join(b.replace("-", " ").replace("_", " ") for b in bits[-2:]).strip().capitalize()


def find_screens(files, texts, fw_of, bound_file, res, php_consts):
    """Pages and screens a user can open, with the front-end files behind each and the endpoints they reach."""
    # WordPress: front-end bundles behind admin screens and shortcodes (wp-scripts / webpack entries).
    entries = {}
    for p, t in texts.items():
        if re.search(r"(?:^|/)webpack\.config\.\w+$", p):
            for m in re.finditer(r"""['"]?([\w-]+)['"]?\s*:\s*(?:path\.(?:resolve|join)\(\s*__dirname\s*,\s*)?['"]\.?/?((?:src|assets)/[^'"]+)['"]""", t):
                q = os.path.normpath(os.path.join(os.path.dirname(p), m.group(2)))
                if q in files:
                    entries[m.group(1)] = q
    for p, f in files.items():  # scripts a PHP file enqueues: plain paths, or build/<entry>.js from a webpack entry
        if ext(p) == ".php":
            pre = f["repo"] + "/" if f.get("repo") else ""
            got = []
            for x in re.findall(r"""['"]([\w./-]+\.(?:js|jsx|ts|tsx))['"]""", texts.get(p, "")):
                q = pre + x.lstrip("./")
                near = os.path.normpath(os.path.join(os.path.dirname(p), x.lstrip("/")))  # plugins_url( 'assets/x.js', __FILE__ )
                b = re.search(r"(?:^|/)build/([\w-]+)\.js$", q)
                got += [q] if q in files else [near] if near in files else [entries[b.group(1)]] if b and b.group(1) in entries else []
            f["enq"] = list(dict.fromkeys(got))
    raw = []  # (framework, route, name, file, roots)

    def add(fw, route, file, name=None, roots=None):
        raw.append((fw, route, name or route_name(route), file, roots if roots is not None else [file]))

    for p, f in files.items():
        if f.get("config") or TEST.search(p):
            continue
        fw, e, text = fw_of(p), ext(p), texts.get(p, "")
        m = re.search(r"(?:^|/)app/(?:(.*)/)?page\.(?:tsx|jsx|js|ts|mdx)$", p)
        if m and "expo-router" not in fw:
            add("next", file_route(m.group(1) or ""), p)
            continue
        m = re.search(r"(?:^|/)pages/(.+)\.(?:tsx|jsx|js|ts)$", p)
        if m and "next" in fw and not m.group(1).startswith("api/") and not os.path.basename(m.group(1)).startswith("_"):
            add("next", file_route(m.group(1)), p)
            continue
        m = re.search(r"(?:^|/)src/routes/(.*?)/?\+page\.svelte$", p)
        if m:
            add("sveltekit", file_route(m.group(1)), p)
            continue
        m = re.search(r"(?:^|/)app/routes/(.+?)(?:/route)?\.(?:tsx|jsx)$", p)
        if m and any(d.startswith("@remix-run/") or d in ("@react-router/dev",) for d in fw) and re.search(r"^\s*export\s+default\b", text, re.M):
            add("remix", remix_route(m.group(1)), p)
            continue
        m = re.search(r"(?:^|/)pages/(.+)\.vue$", p)
        if m and "nuxt" in fw:
            add("nuxt", file_route(m.group(1)), p)
            continue
        m = re.search(r"(?:^|/)src/pages/(.+)\.(?:astro|md|mdx)$", p)
        if m:
            add("astro", file_route(m.group(1)), p)
            continue
        m = re.search(r"(?:^|/)app/(.+)\.(?:tsx|jsx|ts|js)$", p)
        if m and "expo-router" in fw and not re.search(r"(?:^|/)[_+]", m.group(1)):
            add("expo", file_route(m.group(1)), p)
            continue
        for m in re.finditer(r"\bcreateFileRoute\s*\(\s*['\"]([^'\"]+)['\"]", text):
            if not re.search(r"__root|/_[^/]*$", m.group(1)):
                add("tanstack", m.group(1).rstrip("/") or "/", p)
        if e in JS:
            pk = " ".join(f.get("pkgs") or [])
            kind = "react-router" if "react-router" in pk else "vue" if "vue-router" in pk else "angular" if "@angular/router" in pk else ""
            if kind:
                code_routes(kind, p, text, f, bound_file, res, add)
        if e == ".php":
            wp_screens(p, text, f, files, php_consts, add)
    for p, t in texts.items():  # WordPress blocks described by block.json
        if os.path.basename(p) == "block.json" and '"name"' in t:
            try:
                j = json.loads(t)
            except ValueError:
                continue
            d = os.path.dirname(p)
            roots = []
            for k in ("editorScript", "viewScript", "script", "viewScriptModule"):
                for v in (j.get(k) if isinstance(j.get(k), list) else [j.get(k)]):
                    if isinstance(v, str) and v.startswith("file:"):
                        x = os.path.normpath(os.path.join(d, v[5:]))
                        roots += [c for c in (x, re.sub(r"(^|/)build/", r"\1src/", x)) if c in files]
            roots += [c for c in (os.path.join(d, n) for n in ("index.js", "edit.js", "view.js", "index.tsx")) if c in files]
            raw.append(("wp-block", str(j.get("name")), str(j.get("title") or j.get("name")), p, list(dict.fromkeys(roots))))

    merged = {}
    for fw, route, name, file, roots in raw:  # the same page registered twice (a top menu and its first submenu): keep the later label
        key = (fw, route, "" if fw.startswith("wp-") else file)
        if key in merged:
            merged[key][2] = name
        else:
            merged[key] = [fw, route, name, file, roots]
    out, used_ids = [], set()
    for fw, route, name, file, roots in merged.values():
        if fw == "wp-admin" and not [r for r in roots if r != file]:  # the admin app bundle; narrowed to this page's screen component
            admin = [q for k, q in entries.items() if re.search(r"admin|dashboard|settings|editor", k)]
            want = {slug(name).replace("-", ""), route.rsplit("=", 1)[-1].split("-")[-1]}
            own = [q for q in front_tree(admin, files) if os.path.splitext(os.path.basename(q))[0].lower() in want and "/screens/" in "/" + q]
            roots = [file] + (own[:1] or admin)
        sid = slug(route.rsplit("=", 1)[-1] if fw == "wp-admin" else name if fw in ("wp-shortcode", "wp-block") else route) or "home"
        base, n = sid, 2
        while sid in used_ids:
            sid, n = f"{base}-{n}", n + 1
        used_ids.add(sid)
        tree = front_tree(roots, files)
        out.append({"id": sid, "name": name, "route": route, "file": file, "framework": fw,
                    "files": list(dict.fromkeys([file] + tree)),
                    "calls": sorted({c["to"] for q in tree for c in files[q].get("calls", [])})})
    return out


def front_tree(roots, files):
    """Front-end files reachable from roots by imports, stopping at server code."""
    out, todo = [], deque(r for r in roots if r in files)
    seen = set(todo)
    while todo:
        p = todo.popleft()
        out.append(p)
        for u in files[p].get("uses", []):
            if u in seen or ext(u) not in JS or re.search(r"(?:^|/)(?:server|api)/|\.server\.", u):
                continue
            seen.add(u)
            todo.append(u)
    return out


def code_routes(kind, p, text, f, bound_file, res, add):
    """Routes declared in code: <Route path>, createBrowserRouter([...]), Vue Router and Angular route arrays."""
    def comp_file(name=None, spec=None):
        if spec:
            r = res(p, spec)
            return r[0] if r else p
        q = bound_file(p, name) if name else None
        return q or p
    hits = []
    for m in re.finditer(r"<Route\b([^>]*)>", text):
        a = m.group(1)
        pm = re.search(r"\bpath\s*=\s*\{?\s*['\"]([^'\"]*)['\"]", a)
        if not pm:
            continue
        cm = re.search(r"\belement\s*=\s*\{\s*<\s*([A-Z]\w*)|\b[Cc]omponent\s*=\s*\{\s*([A-Z]\w*)", a)
        hits.append((pm.group(1), comp_file(cm and (cm.group(1) or cm.group(2)))))
    if kind != "react-router" or re.search(r"createBrowserRouter|createHashRouter|createMemoryRouter|useRoutes|RouteObject", text):
        ms = list(re.finditer(r"\bpath\s*:\s*['\"]([^'\"]*)['\"]", text))
        for i, m in enumerate(ms):
            win = text[m.end():ms[i + 1].start() if i + 1 < len(ms) else m.end() + 500][:500]
            cm = re.search(r"\belement\s*:\s*<\s*([A-Z]\w*)|\b(?:component|Component|loadComponent)\s*:\s*(?:\(\)\s*=>\s*)?([A-Z]\w*)|import\(\s*['\"]([^'\"]+)['\"]", win)
            if not cm or re.search(r"\b(?:redirect|redirectTo)\s*:", win[:cm.start()]):
                continue
            hits.append((m.group(1), comp_file(cm.group(1) or cm.group(2), cm.group(3))))
    for route, file in hits:
        if route in ("*", "**"):
            continue
        add(kind, "/" + route.lstrip("/"), file)


def wp_screens(p, text, f, files, php_consts, add):
    consts = {**php_consts, **(files[p].get("consts") or {})}
    loops = {}  # foreach ( $pages as $suffix => $label ) over a literal array: expand menu pages registered in the loop
    for m in re.finditer(r"foreach\s*\(\s*\$(\w+)\s+as\s+\$(\w+)\s*=>\s*\$(\w+)\s*\)", text):
        a = re.search(r"\$" + m.group(1) + r"\s*=\s*(?:array\s*\(|\[)", text)
        if a:
            body = call_args(text, a.end())
            pairs = re.findall(r"""['"]([^'"]*)['"]\s*=>\s*(?:\w*__\s*\(\s*)?['"]([^'"]*)['"]""", body)
            if pairs:
                loops[(m.group(2), m.group(3))] = pairs
    enq = f.get("enq", [])
    for s in f.get("screens", []):
        if "wpmenu" in s:
            a = s["args"]
            a = a[1:] if s["wpmenu"] == "submenu" else a
            if len(a) < 4:
                continue
            title, slug_x = a[1], a[3]
            variants = [(title, slug_x)]
            for (kv, vv), pairs in loops.items():
                if "$" + kv in slug_x or "$" + vv in title:
                    variants = [(re.sub(r"\$" + vv + r"\b", repr(v), title), re.sub(r"\$" + kv + r"\b", repr(k), slug_x)) for k, v in pairs]
            for t, sx in variants:
                page = php_expr(sx, consts, text)
                if not page or "*" in page or page.endswith(".php"):
                    continue
                add("wp-admin", "/wp-admin/admin.php?page=" + page, p, php_label(t, consts, text), [p] + enq)
        elif "wpshort" in s and s["wpshort"]:
            tag = php_expr(s["wpshort"][0], consts, text)
            if tag and "*" not in tag:  # scripts it enqueues, here or in the classes it calls
                add("wp-shortcode", "[" + tag + "]", p, f"[{tag}] shortcode", [p] + enq + [q for u in files[p].get("uses", []) for q in files[u].get("enq", [])])
        elif "wpblock" in s and s["wpblock"]:
            name = php_expr(s["wpblock"][0], consts, text)
            if re.fullmatch(r"[\w-]+/[\w-]+", name):
                add("wp-block", name, p, name, [p] + enq)


# ---------------------------------------------------------------- unused files

ENTRY = re.compile(r"(?:^|/)(?:index|main|app|server|cli|bin|manage|wsgi|asgi|setup|settings|conftest|__init__|__main__|uninstall|functions|"
                   r"middleware|instrumentation|proxy|sw|service-worker|worker|_app|_document|root|entry\.\w+|App|layout|template|error|"
                   r"not-found|loading|global-error|routes)\.[\w.]+$|\.d\.ts$|\.config\.\w+$|(?:^|/)(?:\.|_)[^/]*$|"
                   r"(?:^|/)(?:scripts?|bin|migrations?|alembic|db/migrate|seeds?|templates?|views?|partials?|public|static|"
                   r"stubs?|examples?|docs?|prototypes?|languages|lang|locales?|i18n|tools?|experiments?|benchmarks?|fixtures|"
                   r"database|config|Console|Commands|Providers|Policies|Observers|Listeners|Jobs|Events|Mail|Notifications|"
                   r"Http/Middleware|factories|seeders|routes|pages|assets)/|(?:^|/)(?:webpack|gulpfile|gruntfile|rollup|vite|babel|jest|"
                   r"vitest|playwright|karma|eslint|prettier|tailwind|postcss|svelte|astro|nuxt|next|remix|metro|app)\b[^/]*$", re.I)


PY_ENTRY = re.compile(r"(?:^|/)(?:models|admin|apps|tasks|signals|urls|views|celery|forms|serializers|consumers|routing|"
                      r"cli|commands?|plugins?|hooks|fixtures|management)(?:\.py$|/)")


PATH_LIT = re.compile(r"""(?<=[\s'"`(=:,\[])([\w@~./-]*?[\w-]+\.(?:js|jsx|ts|tsx|mjs|cjs|vue|svelte|py|php))(?=[\s'"`)?,;\]]|$)""", re.M)
PY_DOTTED = re.compile(r"""['"]([A-Za-z_]\w*(?:\.\w+)+)(?::\w+)?['"]""")


def find_unused(files, texts, endpoints, screens):
    """Code files nothing imports or names, leaving out entry points."""
    used = {u for f in files.values() for u in f.get("uses", [])}
    keep = {v["file"] for v in endpoints.values()} | {s["file"] for s in screens} | {q for s in screens for q in s["files"]}
    cands = [p for p, f in files.items() if not f.get("config") and not f.get("big") and ext(p) in (JS | {".php", ".py"}) and p not in used
             and p not in keep and not TEST.search(p) and not ENTRY.search(p)]
    if not cands:
        return []
    named, globbed = set(), []
    for p, t in texts.items():  # every path-like string anywhere (package.json scripts, enqueue calls, html, configs)
        e = ext(p)  # imports are already followed; path strings live in configs, manifests, html, php and settings
        if e in JS | {".py"} and not ENTRY.search(p) and "config" not in p or e == ".json" and not re.search(r"(?:package|composer|[tj]sconfig|manifest|config|turbo|nx|project|app|vercel|firebase|block|deno)[\w.-]*\.json$", p):
            continue
        dotted = PY_DOTTED.findall(t) if e in (".py", ".toml", ".cfg", ".ini", ".json", ".yml", ".yaml") else []
        for s in PATH_LIT.findall(t) + dotted:
            bits = s.strip("./").split("/")
            named.add(bits[-1])
            named.add("/".join(bits[-2:]))
            named.add(re.sub(r"\.\w+$", "", "/".join(bits[-2:])))
        if "import.meta.glob" in t or "require.context" in t:
            globbed += [os.path.normpath(os.path.join(os.path.dirname(p), g.split("*")[0])) for g in re.findall(r"(?:import\.meta\.glob|require\.context)\(\s*['\"]([^'\"]+)", t)]
    themes = [(os.path.dirname(p) + "/").lstrip("/") for p, t in texts.items() if os.path.basename(p) == "style.css" and "Theme Name:" in t[:2000]]
    php_words = None
    out = []
    for p in cands:
        base = os.path.basename(p)
        stem = os.path.splitext(base)[0]
        two = "/".join(p.split("/")[-2:])
        if base in named and stem != "index" or two in named or re.sub(r"\.\w+$", "", two) in named or p.replace("/", ".")[:-3] in named:
            continue
        if any(p.startswith(g) for g in globbed) or ext(p) == ".py" and PY_ENTRY.search(p):
            continue
        t = texts.get(p, "")
        if t.startswith("#!") or re.search(r"""__name__\s*==\s*['"]__main__['"]""", t):  # a script run on its own
            continue
        if ext(p) == ".php":
            if re.search(r"^\s*\*?\s*(?:Plugin|Theme|Template) Name\s*:", t[:3000], re.M) or any(p.startswith(d) for d in themes):
                continue
            if php_words is None:
                php_words = {}
                for q, tq in texts.items():
                    if ext(q) == ".php":
                        for w in set(re.findall(r"[A-Z]\w+", tq)):
                            php_words[w] = php_words.get(w, 0) + 1
            if any(php_words.get(c, 0) > 1 for c in re.findall(r"\b(?:class|interface|trait|enum)\s+(\w+)", t)):
                continue
        out.append(p)
    # A package where much of the code looks unused means imports we can't follow (aliases, plugins, codegen): say nothing there.
    # ponytail: ratio guard per package folder; a real module resolver would remove the need.
    roots = sorted({os.path.dirname(p) for p in texts if os.path.basename(p) in ("package.json", "composer.json", "pyproject.toml", "setup.py")} | {""}, key=len, reverse=True)
    root_of = lambda p: next((r for r in roots if not r or p.startswith(r + "/")), "")
    total, flagged = {}, {}
    for p, f in files.items():
        if not f.get("config") and ext(p) in (JS | {".php", ".py"}):
            total[root_of(p)] = total.get(root_of(p), 0) + 1
    for p in out:
        flagged[root_of(p)] = flagged.get(root_of(p), 0) + 1
    return sorted(p for p in out if flagged[root_of(p)] <= max(3, 0.15 * total[root_of(p)]))


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
SEALED = re.compile(r"^\$2[aby]\$|^\$argon2|^ENC\[|^encrypted:|^sops|\$ANSIBLE_VAULT|^vault:")
SECRET_NEED = {"a Stripe live secret key": ("sk_live_",), "a Stripe restricted key": ("rk_live_",), "an AWS access key": ("AKIA",),
               "a GitHub token": ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"), "a Slack token": ("xox",), "a Google API key": ("AIza",),
               "an OpenAI or Anthropic API key": ("sk-",), "a private key": ("PRIVATE KEY",), "a SendGrid API key": ("SG.",)}
SECRET_ANY = re.compile("|".join(p for p, _ in SECRET[:-1]))
PUBLIC_ENV = re.compile(r"\b((?:VITE|NEXT_PUBLIC|EXPO_PUBLIC|REACT_APP|PUBLIC|NUXT_PUBLIC|GATSBY|VUE_APP)_(?:[A-Z0-9]+_)*?(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE)(?:_[A-Z0-9]+)*)\b")
PUBLIC_OK = re.compile(r"PUBLISHABLE|ANON|SUPABASE_KEY$|PUBLIC_KEY$|SITE_KEY|SITEKEY|MEASUREMENT|FIREBASE|MAPBOX|MAPS|POSTHOG|SENTRY|ALGOLIA_SEARCH|SEARCH_KEY|"
                       r"STRIPE_PK|_PK$|RECAPTCHA|TURNSTILE|HCAPTCHA|VAPID|AMPLITUDE|MIXPANEL|SEGMENT|INTERCOM|CLARITY|GA_|GTM|PUSHER_KEY|"
                       r"ABLY_KEY|ONESIGNAL|REVENUECAT|PLAID_PUBLIC|LAUNCHDARKLY|STATSIG|TOKEN_URL|KEY_ID$|CLIENT_KEY$", re.I)
AUTH_FLOW = re.compile(r"(?:^|/)(?:log-?in|log-?out|sign-?in|sign-?out|sign-?up|register|session|refresh|token|callback|oauth|auth/[\w-]+|verify-email|forgot-password|reset-password)(?:/|$)", re.I)
OPEN_ON_PURPOSE = re.compile(r"robots\.txt|llms(?:-full)?\.txt|rss|feed|atom|sitemap|health|healthz|ping|status|version|favicon|manifest|og(?:-image)?|"
                             r"\.well-known|opengraph|twitter-image|icon", re.I)


def health(sc, tracked_files):
    texts, files, issues = sc["_texts"], sc["files"], []

    def add(rule, sev, title, detail, fix, locs):
        if locs:
            for l in locs:
                l.setdefault("node", "file:" + l["file"])
            ident = rule + ":" + hashlib.md5("|".join(sorted({l["file"] for l in locs})).encode()).hexdigest()[:8]
            issues.append({"id": ident, "rule": rule, "severity": sev, "title": title, "detail": detail, "fix": fix, "locations": locs[:25]})

    def grep(pattern, only=None, skip_tests=False, flags=0, need=()):
        paths = [p for p, t in texts.items() if not (only and ext(p) not in only or skip_tests and TEST.search(p) or need and not any(n in t for n in need))]
        out = []
        for p, m in pool_map(_grep_chunk, paths, texts=texts, rx=re.compile(pattern, flags)) if not need else \
                ((p, m) for p in paths for m in re.finditer(pattern, texts[p], flags)):
            t = texts[p]
            out.append({"file": p, "line": lineno(t, m.start()), "text": t[t.rfind("\n", 0, m.start()) + 1:t.find("\n", m.end()) if t.find("\n", m.end()) >= 0 else len(t)].strip()[:160], "m": m})
        return out

    def plural(n, one, many):
        return one if n == 1 else many

    # Secrets in the code.
    found = []
    for pat, what in SECRET:
        for l in grep(pat, need=SECRET_NEED.get(what, ())):
            val = l["m"].group(1) if l["m"].groups() else l["m"].group(0)
            t = texts[l["file"]]
            generic = what == "a hard-coded secret"  # a name like password = '…': skip tests, translations, and values that aren't key-like
            if generic and (TEST.search(l["file"]) or re.search(r"(?:^|/)(?:lang|locales?|i18n|docs?)/|\.md$", l["file"]) or "|" in val or "/" in val
                            or not (re.search(r"\d", val) and re.search(r"[A-Za-z]", val))):
                continue
            if PLACEHOLDER.search(val) or SEALED.search(val) or os.path.basename(l["file"]).startswith(".env") and l["file"] not in tracked_files \
                    or t.startswith("$ANSIBLE_VAULT") or "sops:" in t and "ENC[" in t:
                continue
            l["what"] = what
            if not any(x["file"] == l["file"] and x["line"] == l["line"] for x in found):  # one report per line
                found.append(l)
    add("secret", "high", f"{len(found)} {plural(len(found), 'secret', 'secrets')} written into the code",
        "Anything in the repository is readable by everyone who can read the repository, and stays in git history after it's deleted.",
        "Move these secrets out of the code into environment variables or the platform's secret settings, rotate the exposed keys, and make sure the files that hold them are ignored by git.",
        [{"file": l["file"], "line": l["line"], "text": l["what"]} for l in found])

    envs = [p for p in tracked_files if re.search(r"(^|/)\.env(\.[\w-]+)?$", p) and not p.endswith((".example", ".sample", ".template", ".dist"))]
    add("env-in-git", "high", "A private .env file is committed to git", "Settings files like .env usually hold passwords and keys.",
        "Remove the committed .env files from git (git rm --cached), add .env to .gitignore, and rotate any secrets they held.",
        [{"file": p, "line": 1, "text": "tracked by git"} for p in envs])

    # Secrets put where the browser can read them: VITE_*, NEXT_PUBLIC_* ... are built into the page.
    exposed, names = [], set()
    for p, t in texts.items():
        env = re.search(r"(^|/)\.env(\.[\w-]+)?$", p)
        if not (env or ext(p) in JS) or TEST.search(p) or p.endswith((".example", ".sample", ".template", ".dist")):
            continue
        for m in PUBLIC_ENV.finditer(t):
            n = m.group(1)
            if PUBLIC_OK.search(n) or n in names:
                continue
            if env:
                v = re.match(r"\s*=\s*['\"]?([^'\"\s#]*)", t[m.end():])
                if not v or not v.group(1) or PLACEHOLDER.search(v.group(1)):
                    continue
            elif not re.search(r"(?:import\.meta\.env|process\.env)\.\s*$", t[max(0, m.start() - 20):m.start()]):
                continue
            names.add(n)
            exposed.append({"file": p, "line": lineno(t, m.start()), "text": f"{n} is built into the browser code"})
    add("public-env-secret", "high", f"{len(exposed)} secret{plural(len(exposed), ' is', 's are')} exposed to the browser",
        "Variables starting with VITE_, NEXT_PUBLIC_, EXPO_PUBLIC_, REACT_APP_ or PUBLIC_ are copied into the JavaScript every visitor downloads. A key, token or password there is public.",
        "Move these secrets to server-only environment variables (no public prefix), call the service from server code instead of the browser, and rotate the exposed keys.", exposed)

    # Database and storage rules (Supabase row level security, Firebase rules).
    sql = {p: t for p, t in texts.items() if ext(p) == ".sql" and re.search(r"(^|/)supabase/", p)}
    all_sql = "\n".join(sql.values()).lower()
    norls, openpol = [], []
    for p, t in sql.items():
        for m in re.finditer(r"create\s+table\s+(?:if\s+not\s+exists\s+)?((?:\"?\w+\"?\.)?\"?(\w+)\"?)", t, re.I):
            name = m.group(2).lower()
            if (m.group(1).lower().split(".")[0].strip('"') in ("auth", "storage", "private", "extensions") and "." in m.group(1)):
                continue
            if not re.search(r"alter\s+table\s+(?:only\s+)?(?:if\s+exists\s+)?(?:\"?\w+\"?\.)?\"?" + re.escape(name) + r"\"?\s+enable\s+row\s+level\s+security", all_sql):
                norls.append({"file": p, "line": lineno(t, m.start()), "text": f"table {name} has no row level security"})
        for m in re.finditer(r"create\s+policy\s+[^;]*?\bfor\s+(insert|update|delete|all)\b[^;]*?\b(?:using|with\s+check)\s*\(\s*true\s*\)", t, re.I | re.S):
            openpol.append({"file": p, "line": lineno(t, m.start()), "text": f"anyone may {m.group(1).lower()} rows"})
    add("rls", "high", f"{len(norls)} database table{plural(len(norls), ' has', 's have')} no row level security",
        "In Supabase the browser talks to the database directly; a table without row level security can be read and changed by anyone with the public anon key.",
        "Enable row level security on these tables (alter table … enable row level security) and add policies that only let each user reach their own rows.", norls)
    add("open-policy", "high", f"{len(openpol)} database polic{plural(len(openpol), 'y lets', 'ies let')} anyone change data",
        "A policy with using (true) or with check (true) for insert, update or delete lets every visitor write those rows.",
        "Tighten these policies to check auth.uid() against the row's owner (or a role) instead of true.", openpol)
    rules, signed = [], []
    for p, t in texts.items():
        b = os.path.basename(p)
        if b in ("firestore.rules", "storage.rules"):
            for m in re.finditer(r"allow\s+([\w,\s]+?)\s*:\s*if\s+true\s*;", t):
                rules.append({"file": p, "line": lineno(t, m.start()), "text": f"anyone may {m.group(1).strip()}"})
            for m in re.finditer(r"request\.time\s*<\s*timestamp\.date\(\s*(\d{4})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)", t):
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                rules.append({"file": p, "line": lineno(t, m.start()), "text": f"test mode: open to everyone until {d.isoformat()}" + (" (expired: the app stops working)" if d < date.today() else "")})
            for m in re.finditer(r"match\s+/\{\w+=\*\*\}\s*\{\s*allow\s+[\w,\s]+:\s*if\s+request\.auth\s*!=\s*null\s*;", t):
                signed.append({"file": p, "line": lineno(t, m.start()), "text": "any signed-in user may read and write everything"})
        elif b == "database.rules.json":
            for m in re.finditer(r"\"\.(read|write)\"\s*:\s*(?:true|\"true\")", t):
                rules.append({"file": p, "line": lineno(t, m.start()), "text": f"anyone may {m.group(1)}"})
    add("firebase-rules", "high", "Firebase rules let anyone read or write data", "These rules allow access without signing in (or test-mode rules that open everything until a date).",
        "Rewrite these Firebase rules so each document is only readable and writable by the users who own it (request.auth.uid == resource.data.owner).", rules)
    add("firebase-signed-in", "medium", "Any signed-in user can reach all the data", "A catch-all rule allows every signed-in account to read and write every document, including other people's.",
        "Replace the catch-all request.auth != null rule with per-collection rules that check the user owns the document.", signed)

    # Endpoints anyone can call.
    eps = sc["endpoints"]
    nocheck = [(k, v) for k, v in eps.items() if "checked" in v and not v["checked"]]
    add("wp-no-permission", "high", f"{len(nocheck)} REST route{plural(len(nocheck), ' has', 's have')} no permission_callback",
        "WordPress treats a route without permission_callback as public and logs a notice.",
        "Add a permission_callback to each of these register_rest_route calls: current_user_can() for admin routes, or __return_true plus a nonce check and rate limit for routes that are public on purpose.",
        [{"file": v["file"], "line": v["line"], "text": k, "node": "ep:" + k} for k, v in nocheck])
    pub = [(k, v) for k, v in eps.items() if v.get("auth") == "public" and not ("checked" in v and not v["checked"])]
    writes = [(k, v) for k, v in pub if v["method"] in WRITE_METHODS and not (v["method"] == "ANY" and OPEN_ON_PURPOSE.search(v["path"]))
              and not AUTH_FLOW.search(v["path"])]  # signing in, out or up has to be open
    loud = [(k, v) for k, v in writes if not v.get("nonce")]
    add("public-write", "medium", f"{len(loud)} public endpoint{plural(len(loud), ' changes', 's change')} data without a nonce or signature check",
        "Anyone on the internet can call these, and nothing in the handler's file checks a nonce, token or signature.",
        "Check each of these public endpoints: if it should be public, verify a nonce or signed token and rate-limit it; otherwise require a logged-in user with the right capability.",
        [{"file": v["file"], "line": v["line"], "text": k, "node": "ep:" + k} for k, v in loud])
    add("public-by-design", "low", f"{len(writes) - len(loud)} public endpoint{plural(len(writes) - len(loud), '', 's')} change data (nonce checked)",
        "These are open to anyone but their files check a nonce. Fine if that's the plan; worth a look if not.",
        "Review these public endpoints: confirm each verifies its nonce before acting, validates and sanitizes every field, and is rate-limited.",
        [{"file": v["file"], "line": v["line"], "text": k, "node": "ep:" + k} for k, v in writes if v.get("nonce")])
    reads = [(k, v) for k, v in pub if v["method"] in ("GET", "HEAD") and not OPEN_ON_PURPOSE.search(v["path"]) and not AUTH_FLOW.search(v["path"])
             and not re.search(r"Cache-Control['\"]?\s*[,:]\s*['\"]public", texts.get(v["file"], ""))]
    add("public-read", "low", f"{len(reads)} endpoint{plural(len(reads), ' returns', 's return')} data to anyone",
        "No sign-in is needed to call these. Fine for public content; check none of them return private or per-user data.",
        "Review these public read endpoints: confirm each only returns data meant for everyone, and add an auth check to any that return private data.",
        [{"file": v["file"], "line": v["line"], "text": k, "node": "ep:" + k} for k, v in reads])
    mis = [{"file": p, "line": m["line"], "text": f"auth middleware registered after {len(m['routes'])} route{plural(len(m['routes']), '', 's')} (lines {', '.join(map(str, m['routes'][:5]))})"}
           for p, ms in (sc.get("misplaced") or {}).items() for m in ms]
    add("misplaced-guard", "high", "An auth check is registered after the routes it should protect",
        "Express runs middleware in order: routes added before app.use(auth) never pass through it, so they are open to everyone.",
        "Move the auth middleware above these routes (or add it to each route) so every route registered in this file is protected.", mis)

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
    sqli = [l for l in grep(r"""\$wpdb->(?:query|get_results|get_var|get_row|get_col)\s*\(\s*(?:"[^"]*\$\w|['"][^'"]*['"]\s*\.\s*\$)""", {".php"}, need=("$wpdb",)) if unsafe(l)]
    sqli += grep(r"""\.execute\s*\(\s*(?:f["']|["'][^"']*["']\s*(?:%|\.format|\+))""", {".py"}, need=(".execute",))
    sqli += grep(r"""\.(?:query|raw)\s*\(\s*`[^`]*\$\{""", JS, need=(".query", ".raw"))
    add("sql", "medium", f"{len(sqli)} database quer{plural(len(sqli), 'y is', 'ies are')} built from variables", "Values pasted into SQL can change the query (SQL injection).",
        "Rewrite these queries with placeholders ($wpdb->prepare, parameterised queries) instead of pasting variables into the SQL.", sqli)
    raw = grep(r"""\b(?:echo|print)\s+\$_(?:GET|POST|REQUEST|COOKIE)\b|\bunserialize\s*\(\s*\$_|\bextract\s*\(\s*\$_|\beval\s*\(\s*\$_""", {".php"}, need=("$_",))
    add("raw-input", "high", "Request input is echoed or executed directly", "Printing or unserializing request data as-is allows XSS or code execution.",
        "Sanitize and escape the request input at each of these places (sanitize_text_field / esc_html, json_decode instead of unserialize).", raw)
    evals = grep(r"""(?<![\w.$])eval\s*\(""", JS | {".php", ".py"}, skip_tests=True, need=("eval",))
    add("eval", "medium", f"eval() is used in {len({l['file'] for l in evals})} file{plural(len({l['file'] for l in evals}), '', 's')}", "eval runs any text as code.",
        "Replace these eval() calls with code that doesn't execute strings.", evals)
    html = grep(r"dangerouslySetInnerHTML", JS, skip_tests=True, need=("dangerouslySetInnerHTML",))
    add("raw-html", "low", f"HTML is inserted unescaped in {len({l['file'] for l in html})} place{plural(len(html), '', 's')}", "Fine for trusted content; an XSS risk if any of it comes from users.",
        "Check that every dangerouslySetInnerHTML here only receives trusted or sanitized HTML.", html)

    # Webhooks and CORS.
    hooks = []
    for k, v in eps.items():  # a server route for Stripe's webhook that never verifies the signature
        t = texts.get(v["file"], "")
        if re.search(r"webhook|stripe", k, re.I) and v["method"] in ("POST", "ANY") and re.search(r"stripe", t, re.I) \
                and not re.search(r"constructEvent|construct_event|verify_header|stripe-signature|signature", t, re.I) and not TEST.search(v["file"]):
            hooks.append({"file": v["file"], "line": v["line"], "text": "Stripe webhook without a signature check", "node": "ep:" + k})
    add("webhook", "high", "A payment webhook doesn't check the signature", "Anyone could send a fake 'payment succeeded' event.",
        "Verify the Stripe-Signature header with the webhook secret (stripe.webhooks.constructEvent) before trusting these events.", hooks)
    cors = grep(r"""Access-Control-Allow-Origin['"]?\s*[:,]\s*(?:['"]?\*|(?:req|request)\.headers\.origin|origin\b)|\bcors\(\s*\)|\bcors\(\s*\{[^}]*origin\s*:\s*(?:true|['"]\*['"])|allow_origins\s*=\s*\[\s*['"]\*|CORS_ALLOW_ALL_ORIGINS\s*=\s*True""", CODE, skip_tests=True,
                need=("Access-Control-Allow-Origin", "cors", "allow_origins", "CORS_ALLOW"))
    creds = [l for l in cors if re.search(r"credentials\s*:\s*true|Allow-Credentials['\"]?\s*[:,]\s*['\"]?true|allow_credentials\s*=\s*True|CORS_ALLOW_CREDENTIALS\s*=\s*True", texts[l["file"]], re.I)]
    add("cors-credentials", "medium", "Any website can make signed-in requests to the server", "CORS is open to every origin and allows credentials, so another site could act as a signed-in visitor.",
        "Limit CORS to a list of trusted origins wherever credentials are allowed.", creds)
    add("cors", "low", "Any website can call the server from a visitor's browser", "CORS is open to every origin.",
        "Limit CORS to the origins that need it.", [l for l in cors if l not in creds])

    # Housekeeping.
    big = [{"file": p, "line": 1, "text": f"{f['lines']} lines"} for p, f in files.items() if ext(p) in CODE and f.get("lines", 0) > 1200]
    add("large", "low", f"{len(big)} file{plural(len(big), ' is', 's are')} over 1,200 lines", "Big files are slower to read and change, for people and for Claude.",
        "Suggest how to split these large files into smaller modules, without changing behaviour.", big)
    local = grep(r"""['"`]https?://(?:localhost|127\.0\.0\.1)(?::\d+)?""", CODE, skip_tests=True, need=("localhost", "127.0.0.1"))
    local = [l for l in local if not re.search(r"(config|\.env|vite|webpack|playwright|jest|setup|scripts?/|dev)", l["file"], re.I)]
    add("localhost", "low", "Code points at a localhost address", "These break once the app runs anywhere else.",
        "Replace these hard-coded localhost URLs with configuration.", local)
    unused = sorted(p for p, f in files.items() if f.get("unused"))
    add("unused", "low", f"{len(unused)} code file{plural(len(unused), ' is', 's are')} never imported", "Nothing imports, requires or names these files, and they aren't entry points. They may be dead code.",
        "Check whether these files are still needed: delete the dead ones, or wire up the ones that should be used.",
        [{"file": p, "line": 1, "text": "not imported anywhere"} for p in unused])
    for i in issues:
        for l in i["locations"]:
            l.pop("m", None)
            l.pop("what", None)
            if i["rule"] != "secret" and SECRET_ANY.search(l["text"]):
                l["text"] = "(line hidden: it holds a secret)"
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
                        "low": sum(1 for i in issues if i["severity"] == "low" and not i["dismissed"])}}
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
