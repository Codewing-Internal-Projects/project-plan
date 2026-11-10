"""Per-file extraction for the project map: imports, definitions, endpoints and requests for JS/TS, PHP, Python, Go, Rust and Java,
import resolvers, endpoint matching, and the worker pool that runs extraction over big projects. Part of codemap (import codemap first)."""
import fnmatch
import os
import re

from codemap import CODE, ext, JS, TEST


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
