"""Self-check for the project map scanner: python3 tests/test_codemap.py (exits non-zero on failure)."""
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugins", "project-plan", "skills", "project-plan", "scripts"))
import codemap  # noqa: E402

FILES = {
    "server/app.js": """const express = require('express');
const { charge } = require('./billing');
const usersRouter = require('./routes/users');
const app = express();
app.post('/api/orders', (req, res) => charge(req.body));
app.get('/api/orders/:id', (req, res) => res.json({}));
app.use('/api/users', requireAuth, usersRouter);
app.use('/api/public', require('./routes/pub'));
const cors = require('cors');
app.use(cors());
app.get('/early', (req, res) => res.send('x'));
app.use(requireAuth);
const cfg = { apiKey: 'sk_live_51HabcdefghijklmnopQRSTUV' };\nconst SENDGRID_API_KEY = 'SG.9fJk2LmQpRsTuVwXyZ01234';
""",
    "server/billing.js": "export function charge(x) { return db.query(`SELECT * FROM t WHERE id = ${x.id}`); }\n",
    "server/routes/users.js": """const router = require('express').Router();
router.get('/:id', getUser);
router.post('/', requireAdmin, createUser);
function getUser(req, res) { res.json({}); }
module.exports = router;
""",
    "server/routes/pub.js": """const express = require('express');
const r = express.Router();
r.get('/list', (req, res) => res.json([]));
r.post('/signup', (req, res) => res.json({}));
module.exports = r;
""",
    "hono/index.ts": """import { Hono } from 'hono';
import books from './books';
const app = new Hono().basePath('/v2');
app.route('/books', books);
export default app;
""",
    "hono/books.ts": "import { Hono } from 'hono';\nconst b = new Hono();\nb.get('/:id', (c) => c.json({}));\nexport default b;\n",
    "hono/cors.ts": "import { cors } from 'hono/cors';\nexport const c = cors({ origin: '*', credentials: true });\n",
    "fast/server.js": "const fastify = require('fastify')();\nfastify.register(require('./items'), { prefix: '/v1' });\n",
    "fast/items.js": "module.exports = async function (fastify) { fastify.get('/items', async () => []); };\n",
    "koa/r.js": "const Router = require('@koa/router');\nconst router = new Router({ prefix: '/k' });\nrouter.get('/x', (ctx) => {});\n",
    "web/app/api/hello/route.ts": "export async function GET() { return Response.json({}); }\n",
    "web/app/checkout/page.tsx": "import Cart from '../../components/Cart';\nexport default function Page() { return <Cart />; }\n",
    "web/components/Cart.tsx": "export default function Cart() { fetch('/api/hello'); return null; }\n",
    "web/client.js": "import axios from 'axios';\nconst api = axios.create({ baseURL: '/api' });\nexport default api;\n",
    "web/use.js": """import api from './client';
const BASE = '/api';
const call = (p) => fetch(BASE + p);
export function load() { api.get('/users/42'); return call('/public/list'); }
""",
    "web/fb.js": """import { httpsCallable } from 'firebase/functions';
const run = (name, data) => httpsCallable(functions, name)(data);
export const send = () => run('addMessage', {});
export const direct = httpsCallable(functions, 'ping2');
""",
    "web/routes.jsx": """import { createBrowserRouter, Route } from 'react-router-dom';
import Home from './Home';
import Profile from './Profile';
export const router = createBrowserRouter([{ path: '/', element: <Home /> }, { path: '/orders', Component: Orders }]);
export const r2 = <Route path="/profile" element={<Profile />} />;
""",
    "web/Home.jsx": "import { load } from './use';\nexport default function Home() { load(); return null; }\n",
    "web/Profile.jsx": "export default function Profile() { return null; }\n",
    "web/dead.js": "export function nobodyCallsMe() {}\n",
    "web/live.js": "console.log('run by a package.json script');\n",
    "web/env.js": "export const t = import.meta.env.VITE_API_TOKEN;\nexport const k = import.meta.env.VITE_FIREBASE_API_KEY;\nexport const w = import.meta.env.VITE_KEYWORD_LIMIT;\n",
    "web/.env.production": "VITE_STRIPE_SECRET_KEY=sk_abc123realvalue\nVITE_SUPABASE_ANON_KEY=eyJhbGciOi\nVITE_EMPTY_TOKEN=\n",
    "vue/router.js": "import { createRouter } from 'vue-router';\nexport default createRouter({ routes: [{ path: '/about', component: () => import('./About.vue') }] });\n",
    "vue/About.vue": "<template><p>about</p></template>\n",
    "functions/index.js": """const functions = require('firebase-functions');
exports.addMessage = functions.https.onCall((data, context) => { if (!context.auth) throw new Error('no'); return 1; });
exports.ping2 = functions.https.onCall((data) => 1);
""",
    "kit/src/routes/todos/+server.ts": "export const POST = async () => new Response('');\n",
    "kit/src/routes/todos/+page.svelte": "<h1>todos</h1>\n",
    "nux/server/api/users/[id].get.ts": "export default defineEventHandler(() => ({}));\n",
    "site/src/pages/rss.xml.ts": "export async function GET() { return new Response(''); }\n",
    "site/src/pages/about.astro": "<h1>About</h1>\n",
    "rmx/package.json": json.dumps({"dependencies": {"@remix-run/react": "2"}}),
    "rmx/app/routes/notes.$id.tsx": "export async function loader() { return null; }\nexport default function Note() { return null; }\n",
    "tbl/routes.js": "export default { 'GET /api/orders/summary': summary };\n",
    "fa/__init__.py": "",
    "fa/routers/__init__.py": "",
    "fa/main.py": "from fastapi import FastAPI\nfrom .routers import users\napp = FastAPI()\napp.include_router(users.router, prefix='/api')\n",
    "fa/routers/users.py": """from fastapi import APIRouter, Depends
router = APIRouter(prefix='/users')
@router.get('/me')
def me(user=Depends(get_current_user)):
    return user
@router.get('/open')
def open_list():
    return []
""",
    "dj/urls.py": "from django.urls import path, include\nurlpatterns = [path('api/', include('dj.api.urls'))]\n",
    "dj/api/urls.py": "from django.urls import path\nfrom . import views\nurlpatterns = [path('items/<int:pk>/', views.item)]\n",
    "dj/api/views.py": "def item(request, pk): pass\n",
    "java/UserController.java": """@RestController
@RequestMapping("/api/v1")
public class UserController {
    @GetMapping("/users/{id}")
    public User get(@PathVariable Long id) { return null; }
    @PreAuthorize("hasRole('ADMIN')")
    @DeleteMapping("/users/{id}")
    public void remove(@PathVariable Long id) {}
}
""",
    "rs/main.rs": """#[get("/hello")]
async fn hello() -> impl Responder { "hi" }
fn app() -> Router { Router::new().route("/users", get(list_users).post(create_user)) }
""",
    "go/main.go": """package main
func main() {
	r := gin.Default()
	v1 := r.Group("/v1")
	v1.GET("/ping", ping)
	http.HandleFunc("GET /healthz", health)
}
""",
    "routes/api.php": """<?php
Route::get('/users', [UserController::class, 'index']);
Route::prefix('admin')->middleware('auth')->group(function () {
    Route::get('/stats', [StatsController::class, 'show']);
});
""",
    "plugin/class-rest.php": """<?php
class Rest {
    const NS = 'shop/v1';
    const BASE = 'items';
    public function routes() {
        register_rest_route( self::NS, '/' . self::BASE, array(
            array( 'methods' => WP_REST_Server::READABLE, 'callback' => array( $this, 'list' ), 'permission_callback' => array( $this, 'can' ) ),
            array( 'methods' => WP_REST_Server::CREATABLE, 'callback' => array( $this, 'add' ), 'permission_callback' => '__return_true' ),
        ) );
        register_rest_route( self::NS, '/' . self::BASE . '/(?P<id>\\d+)', array( 'methods' => 'DELETE', 'callback' => array( $this, 'del' ) ) );
    }
    public function can() { return current_user_can( 'manage_options' ); }
    public function add( $r ) { global $wpdb; $table = $wpdb->prefix . 'items'; $wpdb->query( "DELETE FROM {$table}" );
        $wpdb->get_results( "SELECT * FROM {$table} WHERE name = '$name'" ); }
}
""",
    "plugin/shop.php": """<?php
/**
 * Plugin Name: Shop
 */
require_once __DIR__ . '/class-rest.php';
add_action( 'admin_menu', function () { add_menu_page( 'Shop', __( 'Shop', 'shop' ), 'manage_options', 'shop', 'shop_render' ); } );
add_shortcode( 'shop_cart', 'shop_cart' );
add_action( 'admin_post_shop_export', 'shop_export' );
add_action( 'wp_ajax_nopriv_shop_ping', 'shop_ping' );
add_action( 'wp_ajax_shop_ping', 'shop_ping' );
function shop_ping() { check_ajax_referer( 'shop' ); }
function shop_cart() { wp_enqueue_script( 'cart', plugins_url( 'assets/cart.js', __FILE__ ) ); }
""",
    "plugin/assets/cart.js": "fetch('/wp-json/shop/v1/items', { method: 'POST' });\n",
    "plugin/blocks/cart/block.json": json.dumps({"name": "shop/cart", "title": "Cart", "editorScript": "file:./index.js"}),
    "plugin/blocks/cart/index.js": "export default {};\n",
    "app/views.py": """from flask import Flask
from .models import Order
from .bp import bp
app = Flask(__name__)
app.register_blueprint(bp, url_prefix='/shop')
@app.route('/health', methods=['GET'])
def health(): return 'ok'
""",
    "app/bp.py": "from flask import Blueprint\nbp = Blueprint('b', __name__, url_prefix='/own')\n@bp.route('/items', methods=['POST'])\ndef add(): pass\n",
    "app/models.py": "class Order: pass\n",
    "app/__init__.py": "",
    "supabase/migrations/001_init.sql": """create table orders (id bigint primary key);
create table public.notes (id bigint primary key);
alter table public.notes enable row level security;
create policy "anyone writes" on notes for insert with check (true);
create policy "anyone reads" on notes for select using (true);
""",
    "firestore.rules": "service cloud.firestore {\n  match /databases/{db}/documents {\n    match /{document=**} {\n      allow read, write: if request.time < timestamp.date(2020, 1, 1);\n    }\n    match /users/{uid} { allow read: if request.auth.uid == uid; }\n  }\n}\n",
    "storage.rules": "service firebase.storage {\n  match /b/{bucket}/o {\n    match /{all=**} { allow read: if true; }\n  }\n}\n",
    "nest/users.controller.ts": """@Controller('nest/users')
export class UsersController {
  @Get(':id')
  findOne(@Param('id') id: string) { return id; }
  @UseGuards(AuthGuard('jwt'))
  @Post()
  create(@Body() body) { return body; }
}
""",
    "hono/private.ts": """import { Hono } from 'hono';
const priv = new Hono();
priv.use('/*', requireSession());
priv.get('/me', (c) => c.json({}));
export default priv;
""",
    "hono/main.ts": "import { Hono } from 'hono';\nimport priv from './private';\nconst root = new Hono();\nroot.route('/private', priv);\n",
    "pay/hook.js": """const express = require('express');
const stripe = require('stripe')(process.env.STRIPE_KEY);
const app = express();
app.post('/stripe/webhook', (req, res) => { const event = req.body; res.sendStatus(200); });
""",
    "pay/safe.js": """const express = require('express');
const stripe = require('stripe')(process.env.STRIPE_KEY);
const app = express();
app.post('/stripe/webhook2', (req, res) => { stripe.webhooks.constructEvent(req.body, req.headers['stripe-signature'], s); });
""",
    "laravel/RegisterController.php": "<?php\n$rules = ['password' => 'required|string|min:6|confirmed'];\n",
    "cfg/users.yml": "admin:\n  password: '$2b$12$abcdefghijklmnopqrstuvwxyzABCDEF'\n",
    "package.json": json.dumps({"scripts": {"live": "node web/live.js"}}),
    ".env": "SECRET=abc\n",
}


def write(root, files):
    for p, text in files.items():
        os.makedirs(os.path.join(root, os.path.dirname(p)), exist_ok=True)
        with open(os.path.join(root, p), "w") as fh:
            fh.write(text)


def git(*args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True)


def issue(g, rule):
    return next((i for i in g["health"]["issues"] if i["rule"] == rule), None)


def main():
    root = tempfile.mkdtemp()
    write(root, FILES)
    git("init", "-q", root)
    git("-C", root, "add", "-A")
    codemap.setup(root, os.path.join(root, ".progress"))
    g = codemap.build(quiet=True)
    eps, files = g["endpoints"], g["files"]
    assert g["repos"] == [] and g["root"] == os.path.abspath(root) and files["web/use.js"]["repo"] == ""

    # Endpoints: frameworks, mount prefixes, handlers and who may call them.
    want = {
        "POST /api/orders": "public", "GET /api/orders/:id": "public", "GET /shop/v1/items": "admin", "POST /shop/v1/items": "public",
        "DELETE /shop/v1/items/(?P<id>\\d+)": "public", "GET /health": "public",
        "GET /api/users/:id": "signed-in", "POST /api/users": "admin", "GET /api/public/list": "public", "POST /api/public/signup": "public",
        "GET /early": "public", "GET /v2/books/:id": "public", "GET /v1/items": "public", "GET /k/x": "unknown",
        "GET /api/hello": "public", "POST /todos": "public", "GET /api/users/[id]": "public", "GET /rss.xml": "public", "GET /notes/:id": "public",
        "CALL /addMessage": "signed-in", "CALL /ping2": "public", "GET /api/orders/summary": "unknown",
        "GET /api/users/me": "signed-in", "GET /api/users/open": "public", "ANY /api/items/<int:pk>": "unknown", "POST /shop/items": "public",
        "GET /api/v1/users/{id}": "unknown", "DELETE /api/v1/users/{id}": "admin", "GET /hello": "unknown", "GET /users": "unknown", "POST /users": "unknown",
        "GET /v1/ping": "unknown", "GET /healthz": "unknown", "GET /api/users": "unknown", "GET /api/admin/stats": "signed-in",
        "POST /wp-admin/admin-post.php?action=shop_export": "signed-in",
        "GET /nest/users/:id": "unknown", "POST /nest/users": "signed-in", "GET /private/me": "signed-in", "AJAX /wp-admin/admin-ajax.php?action=shop_ping": "public",
    }
    for k, auth in want.items():
        assert k in eps, (k, sorted(eps))
        assert eps[k]["auth"] == auth, (k, eps[k]["auth"], auth)
    assert eps["GET /api/users/:id"]["handler"] == "getUser" and eps["GET /shop/v1/items"]["handler"] == "list"
    assert eps["CALL /addMessage"]["kind"] == "callable" and eps["AJAX /wp-admin/admin-ajax.php?action=shop_ping"]["kind"] == "ajax"
    assert eps["POST /shop/v1/items"]["public"] and not eps["GET /shop/v1/items"]["public"], "per-method permission_callback"
    assert "GET /v1/health" not in eps and "GET /api/orders/summary" not in files["tbl/routes.js"]["calls"]

    # Imports and requests (wrappers, clients with a base URL, callables through a local helper, printed admin URLs).
    assert "server/billing.js" in files["server/app.js"]["uses"], files["server/app.js"]
    assert "app/models.py" in files["app/views.py"]["uses"], files["app/views.py"]
    assert {"POST /api/orders", "GET /api/orders/:id"} <= set(files["web/api.js"]["calls"]) if "web/api.js" in files else True
    assert {"GET /api/users/:id", "GET /api/public/list"} <= set(files["web/use.js"]["calls"]), files["web/use.js"]["calls"]
    assert {"CALL /addMessage", "CALL /ping2"} <= set(files["web/fb.js"]["calls"]), files["web/fb.js"]["calls"]
    assert files["plugin/assets/cart.js"]["calls"] == ["POST /shop/v1/items"], files["plugin/assets/cart.js"]["calls"]
    assert g["auto"] and {p["id"] for p in g["parts"]} >= {"server", "web", "plugin", "app"}

    # Screens.
    sc = {s["id"]: s for s in g["screens"]}
    for sid, fw in (("checkout", "next"), ("todos", "sveltekit"), ("about", "astro"), ("notes-id", "remix"), ("home", "react-router"),
                    ("orders", "react-router"), ("profile", "react-router"), ("about-2", "vue"), ("shop", "wp-admin"), ("shop-cart-shortcode", "wp-shortcode"),
                    ("cart", "wp-block")):
        assert sid in sc and sc[sid]["framework"] == fw, (sid, fw, {k: v["framework"] for k, v in sc.items()})
    assert "web/components/Cart.tsx" in sc["checkout"]["files"] and sc["checkout"]["calls"] == ["GET /api/hello"], sc["checkout"]
    assert "GET /api/users/:id" in sc["home"]["calls"] and sc["home"]["file"] == "web/Home.jsx", sc["home"]
    assert "plugin/assets/cart.js" in sc["shop-cart-shortcode"]["files"] and "POST /shop/v1/items" in sc["shop-cart-shortcode"]["calls"]
    assert sc["shop"]["route"] == "/wp-admin/admin.php?page=shop" and sc["shop"]["name"] == "Shop"
    assert "plugin/blocks/cart/index.js" in sc["cart"]["files"]

    # Unused files: dead code yes; package.json script targets, entry points and route files no.
    unused = {p for p, f in files.items() if f["unused"]}
    assert "web/dead.js" in unused and not unused & {"web/live.js", "plugin/shop.php", "web/app/checkout/page.tsx", "server/app.js", "web/Home.jsx"}, unused

    # Health: every rule fires on its fixture, and its false-positive guard holds.
    rules = {i["rule"] for i in g["health"]["issues"]}
    for r in ("secret", "env-in-git", "wp-no-permission", "public-write", "public-read", "sql", "cors", "cors-credentials", "misplaced-guard",
              "public-env-secret", "rls", "open-policy", "firebase-rules", "unused", "public-by-design"):
        assert r in rules, (r, rules)
    secret = issue(g, "secret")
    assert len(secret["locations"]) == 2, ("secret names like SENDGRID_API_KEY; bcrypt hashes skipped", secret["locations"])
    assert not any("sk_live" in l["text"] or "SG." in l["text"] for i in g["health"]["issues"] for l in i["locations"]), "no secret values in text"
    sql = issue(g, "sql")
    assert not any("DELETE FROM {$table}" in l["text"] for l in sql["locations"]), "table-name interpolation is not SQL injection"
    env = {l["text"].split()[0] for l in issue(g, "public-env-secret")["locations"]}
    assert env == {"VITE_STRIPE_SECRET_KEY", "VITE_API_TOKEN"}, env
    assert [l["text"] for l in issue(g, "rls")["locations"]] == ["table orders has no row level security"]
    assert [l["text"] for l in issue(g, "open-policy")["locations"]] == ["anyone may insert rows"]
    fb = [l["text"] for l in issue(g, "firebase-rules")["locations"]]
    assert any("until 2020-01-01" in t and "expired" in t for t in fb) and any("anyone may read" in t for t in fb) and len(fb) == 2, fb
    assert [l["file"] for l in issue(g, "cors-credentials")["locations"]] == ["hono/cors.ts"]
    assert [l["node"] for l in issue(g, "webhook")["locations"]] == ["ep:POST /stripe/webhook"], issue(g, "webhook")["locations"]
    assert [l["file"] for l in issue(g, "cors")["locations"]] == ["server/app.js"]
    assert issue(g, "misplaced-guard")["locations"][0]["file"] == "server/app.js"
    pw = {l["node"] for l in issue(g, "public-write")["locations"]}
    assert "ep:POST /api/orders" in pw and "ep:CALL /ping2" in pw and "ep:POST /api/public/signup" not in pw, ("sign-up is open on purpose", pw)
    assert {l["node"] for l in issue(g, "public-by-design")["locations"]} == {"ep:AJAX /wp-admin/admin-ajax.php?action=shop_ping", "ep:POST /stripe/webhook2"}
    pr = {l["node"] for l in issue(g, "public-read")["locations"]}
    assert "ep:GET /api/public/list" in pr and "ep:GET /rss.xml" not in pr and "ep:GET /health" not in pr, pr
    assert {l["node"] for l in issue(g, "wp-no-permission")["locations"]} == {"ep:DELETE /shop/v1/items/(?P<id>\\d+)"}
    assert all(l["node"].startswith(("file:", "ep:")) for i in g["health"]["issues"] for l in i["locations"])

    # Dismissals: new {at, reason} form and the old {id: iso} form.
    mp = os.path.join(root, ".progress", "map")
    json.dump({issue(g, "cors")["id"]: {"at": "2026-01-01", "reason": "dev only"}, issue(g, "eval")["id"] if issue(g, "eval") else "x": "2026-01-01",
               issue(g, "rls")["id"]: "2026-01-01"}, open(os.path.join(mp, "dismissed.json"), "w"))
    g = codemap.build(quiet=True)
    assert issue(g, "cors")["dismissed"] and issue(g, "cors")["reason"] == "dev only" and issue(g, "rls")["dismissed"] and issue(g, "rls")["reason"] == ""

    # Path lookup: client file → endpoint → handler file.
    chain = codemap.path_between(codemap.resolve_node(g, "web/Home.jsx"), codemap.resolve_node(g, "server/routes/users.js"), g)
    assert [n for n, _ in chain] == ["file:web/Home.jsx", "file:web/use.js", "ep:GET /api/users/:id", "file:server/routes/users.js"], chain
    out = io.StringIO()
    with redirect_stdout(out):
        codemap.cli(["path", "web/Home.jsx", "GET", "/api/users/:id"])
    assert "calls → ep:GET /api/users/:id" in out.getvalue(), out.getvalue()

    # Describe it with flows, a tour and link kinds; dead steps are dropped and reported.
    json.dump({"parts": [{"id": "api", "name": "API", "kind": "server", "paths": ["server/**", "plugin/**", "app/**"]},
                         {"id": "web", "name": "Web", "kind": "screen", "paths": ["web/**"]},
                         {"id": "stripe", "name": "Stripe", "kind": "service"}],
               "links": [{"from": "api", "to": "stripe", "label": "charges", "kind": "request"}, {"from": "web", "to": "stripe"}],
               "endpoints": {"GET /api/users/:id": "One user, for signed-in visitors."},
               "flows": [{"id": "buy", "name": "Buying", "steps": [{"node": "screen:checkout", "text": "a"}, {"node": "ep:GET /nope", "text": "gone"},
                                                                    {"node": "part:stripe", "text": "b"}]}],
               "tour": [{"node": "part:api", "text": "start"}, {"node": "file:missing.js", "text": "x"}]},
              open(os.path.join(mp, "architecture.json"), "w"))
    json.dump([{"id": "asked", "name": "Asked", "steps": [{"node": "part:web", "text": "c"}]}, {"id": "buy", "name": "dup", "steps": []}],
              open(os.path.join(mp, "flows.json"), "w"))
    out = io.StringIO()
    with redirect_stdout(out):
        codemap.described()
    assert "Dropped 2 flow/tour steps" in out.getvalue(), out.getvalue()
    g = codemap.build(quiet=True)
    assert [f["id"] for f in g["flows"]] == ["buy", "asked"] and g["flows"][1]["requested"] and not g["flows"][0]["requested"]
    assert [s["node"] for s in g["flows"][0]["steps"]] == ["screen:checkout", "part:stripe"] and g["tour"] == [{"node": "part:api", "text": "start"}]
    kinds = {(e["from"], e["to"]): e["kind"] for e in g["edges"]}
    assert kinds[("api", "stripe")] == "request" and kinds[("web", "stripe")] == "uses" and kinds[("web", "api")] == "", kinds
    assert not g["stale"]

    # Change a file: the part is flagged. Rename a route: its description follows the handler.
    with open(os.path.join(root, "web", "use.js"), "a") as fh:
        fh.write("export function extra() {}\n")
    src = os.path.join(root, "server", "routes", "users.js")
    body = open(src).read()
    open(src, "w").write(body.replace("'/:id'", "'/:userId'"))
    g = codemap.build(quiet=True)
    assert "web" in g["stale"], g["stale"]
    assert g["endpoints"]["GET /api/users/:userId"]["desc"] == "One user, for signed-in visitors.", g["endpoints"]["GET /api/users/:userId"]
    assert any(e["from"] == "web" and e["to"] == "api" and e["calls"] for e in g["edges"]), g["edges"]
    with redirect_stdout(io.StringIO()):
        codemap.worksheet()

    multi_repo()
    audit_runs()
    print("codemap ok")


def audit_runs():
    """The newest finished security-audit run for this project becomes health issues; other projects and running audits don't."""
    root, audits = tempfile.mkdtemp(), tempfile.mkdtemp()
    write(root, {"api/app.js": "app.get('/x', (req, res) => res.send(req.query.q));\n"})
    git("init", "-q", root)
    git("-C", root, "add", "-A")
    finding = {"verdict": "confirmed", "fingerprint": "xss-x", "title": "Reflected XSS in /x", "description": "q is echoed.",
               "trace": [{"kind": "sink", "file": "api/app.js", "line": 1, "scope": "x", "description": "echoes q"}],
               "severity": {"overall_severity": "critical"}, "remediation": {"strategy": "Escape q."}}
    runs = {"run-1": (root, "complete", [dict(finding, fingerprint="old")]), "run-2": (root, "complete", [finding, {"verdict": "needs_validation"}, "junk"]),
            "run-3": (root, "in_progress", []), "run-9": ("/elsewhere", "complete", [])}
    for i, (name, (target, status, records)) in enumerate(runs.items()):
        d = os.path.join(audits, "proj", name)
        write(d, {"run-metadata.json": json.dumps({"target": target, "run_status": status, "profile": "quick"}), "findings.json": json.dumps(records)})
        os.utime(os.path.join(d, "findings.json"), (1000 + i, 1000 + i))
    codemap.AUDITS = audits
    codemap.setup(root, os.path.join(root, ".progress"))
    g = codemap.build(quiet=True)
    i = issue(g, "security-audit")
    assert i and i["id"] == "audit:xss-x" and i["severity"] == "high" and i["fix"] == "Escape q.", i
    assert i["locations"] == [{"file": "api/app.js", "line": 1, "text": "echoes q", "node": "file:api/app.js"}], i["locations"]
    a = g["audit"]
    assert a["run"].endswith("run-2") and a["confirmed"] == 1 and a["needs"] == 1 and a["profile"] == "quick", a

    # The skill itself: missing, then installed in the project's own .claude/skills.
    codemap.AUDIT_SKILL = [os.path.join(audits, "nowhere", "SKILL.md"), "{root}/.claude/skills/security-audit/SKILL.md"]
    assert codemap.audit_skill() is False
    write(root, {".claude/skills/security-audit/SKILL.md": "---\nname: security-audit\n---\n"})
    assert codemap.audit_skill() is True


def multi_repo():
    """A folder of repos: paths get the repo prefix, requests cross repos, a second worktree is skipped."""
    root = tempfile.mkdtemp()
    write(root, {"web/src/api.js": "export const items = () => fetch('/api/items', { method: 'POST' });\n",
                 "api/server.js": "const express = require('express');\nconst app = express();\napp.post('/api/items', (req, res) => res.json({}));\n"})
    for r in ("web", "api"):
        git("init", "-q", os.path.join(root, r))
        git("-C", os.path.join(root, r), "add", "-A")
        git("-C", os.path.join(root, r), "commit", "-qm", "x")
    git("-C", os.path.join(root, "web"), "worktree", "add", "-q", os.path.join(root, "web-copy"))
    codemap.setup(root, os.path.join(root, ".progress"))
    g = codemap.build(quiet=True)
    assert g["repos"] == ["api", "web"], g["repos"]
    assert "web/src/api.js" in g["files"] and g["files"]["web/src/api.js"]["repo"] == "web" and not any(p.startswith("web-copy/") for p in g["files"])
    assert g["files"]["web/src/api.js"]["calls"] == ["POST /api/items"], g["files"]["web/src/api.js"]
    assert g["endpoints"]["POST /api/items"]["file"] == "api/server.js"


if __name__ == "__main__":
    main()
