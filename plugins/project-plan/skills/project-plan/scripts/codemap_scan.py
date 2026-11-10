"""The free scan behind the project map: reads every tracked file, links imports, endpoints, requests and screens. Part of codemap (import codemap first)."""
import hashlib
import json
import os
import re
from collections import deque

import codemap
from codemap import CODE, ext, find_repos, JS, list_files, MAX_BYTES, now, slug, STYLE, TEST
from codemap_extract import (
    ADMIN_CAPS, AUTH_NAMES, body_auth, call_args, eval_js, extract_all, file_route, HANDLER_HINT, join_path, JSLIT, lineno, lit,
    match_score, METHOD, names_in, NONCE, php_expr, php_label, remix_route, resolve_js, resolve_php, resolve_py, segs, SESSION, split_top,
    STR, WP_METHODS)
from codemap_health import find_unused


FRAMEWORKS = ("next", "nuxt", "astro", "@sveltejs/kit", "expo-router", "react-router", "react-router-dom", "vue-router", "@angular/router",
              "@tanstack/react-router", "@remix-run/react", "@remix-run/node", "@react-router/dev", "express", "hono", "fastify")


def scan():
    repos = find_repos()
    paths = list_files(repos)
    files, texts, raws = {}, {}, {}
    for p in paths:
        try:
            full = os.path.join(codemap.ROOT, p)
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
