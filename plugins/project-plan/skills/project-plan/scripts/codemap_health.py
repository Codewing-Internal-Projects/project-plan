"""The map's health check: unused files, secrets in the code, open endpoints, big files and the security-audit findings. Part of codemap (import codemap first)."""
import glob
import hashlib
import os
import re
from datetime import date, datetime, timezone

import codemap
from codemap import CODE, ext, JS, rj, TEST, WRITE_METHODS
from codemap_extract import _grep_chunk, lineno, pool_map


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


AUDITS = os.path.expanduser("~/security-audit-skill")  # where the security-audit skill writes its runs by default
AUDIT_SEV = {"critical": "high", "high": "high", "medium": "medium", "low": "low", "informational": "low"}
AUDIT_SKILL = ["~/.claude/skills/security-audit/SKILL.md", "~/.agents/skills/security-audit/SKILL.md",  # by hand, or `npx skills add`
               "~/.claude/plugins/cache/*/*/*/skills/security-audit/SKILL.md", "{root}/.claude/skills/security-audit/SKILL.md"]


def audit_skill():
    """Whether Claude Code can load Cloudflare's security-audit skill here (globally, from a plugin, or in this project)."""
    root = glob.escape(os.path.abspath(codemap.ROOT))
    return any(glob.glob(os.path.expanduser(p.format(root=root))) for p in AUDIT_SKILL)


def audit():
    """The newest finished security-audit run for this project: (its confirmed findings as health issues, a summary or None)."""
    root, runs = os.path.abspath(codemap.ROOT), []
    obj = lambda x: x if isinstance(x, dict) else {}  # noqa: E731
    for meta in glob.glob(os.path.join(AUDITS, "*", "run-*", "run-metadata.json")):
        m, run = rj(meta, {}), os.path.dirname(meta)
        target = os.path.abspath(str(m.get("target") or "")) if isinstance(m, dict) else ""
        found = os.path.join(run, "findings.json")
        if (target == root or target.startswith(root + os.sep)) and m.get("run_status") != "in_progress" and os.path.isfile(found):
            runs.append((os.path.getmtime(found), run, m, target))
    if not runs:
        return [], None
    when, run, m, target = max(runs)
    records = rj(os.path.join(run, "findings.json"), [])
    records = [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []
    issues = []
    for r in records:
        if r.get("verdict") != "confirmed":
            continue
        locs = []
        for t in r.get("trace") if isinstance(r.get("trace"), list) else []:
            f = str(t.get("file") or "") if isinstance(t, dict) else ""
            if f:
                rel = os.path.relpath(f if os.path.isabs(f) else os.path.join(target, f), root)
                if not rel.startswith(".."):
                    locs.append({"file": rel, "line": t["line"] if isinstance(t.get("line"), int) else 0, "text": str(t.get("description") or "")[:160], "node": "file:" + rel})
        sev = AUDIT_SEV.get(str(obj(r.get("severity")).get("overall_severity")), "medium")
        issues.append({"id": "audit:" + str(r.get("fingerprint") or r.get("title"))[:120], "rule": "security-audit", "severity": sev,
                       "title": str(r.get("title") or "Security finding"), "detail": str(r.get("description") or "")[:600],
                       "fix": str(obj(r.get("remediation")).get("strategy") or ""), "locations": locs[:25]})
    report = os.path.join(run, "REPORT.md")
    return issues, {"run": run, "when": datetime.fromtimestamp(when, timezone.utc).isoformat(timespec="seconds"),
                    "status": str(m.get("run_status") or ""), "profile": str(m.get("profile") or ""), "ref": str(m.get("source_ref") or "")[:80],
                    "confirmed": len(issues), "needs": sum(1 for r in records if r.get("verdict") == "needs_validation"),
                    "report": report if os.path.isfile(report) else ""}


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
