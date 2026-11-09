"""Self-check for the project map scanner: python3 tests/test_codemap.py (exits non-zero on failure)."""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugins", "project-plan", "skills", "project-plan", "scripts"))
import codemap  # noqa: E402

FILES = {
    "server/app.js": """const express = require('express');
const { charge } = require('./billing');
const app = express();
app.post('/api/orders', (req, res) => charge(req.body));
app.get('/api/orders/:id', (req, res) => res.json({}));
const cors = require('cors');
app.use(cors());
const cfg = { apiKey: 'sk_live_51HabcdefghijklmnopQRSTUV' };\nconst SENDGRID_API_KEY = 'SG.9fJk2LmQpRsTuVwXyZ01234';
""",
    "server/billing.js": "export function charge(x) { return db.query(`SELECT * FROM t WHERE id = ${x.id}`); }\n",
    "web/api.js": """export const ORDERS = '/api/orders';
export function load(id) { return fetch(`${ORDERS}/${id}`); }
export function save(o) { return fetch('/api/orders', { method: 'POST', body: JSON.stringify(o) }); }
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
    public function add( $r ) { global $wpdb; $table = $wpdb->prefix . 'items'; $wpdb->query( "DELETE FROM {$table}" );
        $wpdb->get_results( "SELECT * FROM {$table} WHERE name = '$name'" ); }
}
""",
    "app/views.py": """from flask import Flask
from .models import Order
app = Flask(__name__)
@app.route('/health', methods=['GET'])
def health(): return 'ok'
""",
    "app/models.py": "class Order: pass\n",
    "app/__init__.py": "",
    ".env": "SECRET=abc\n",
}


def main():
    root = tempfile.mkdtemp()
    for p, text in FILES.items():
        os.makedirs(os.path.join(root, os.path.dirname(p)), exist_ok=True)
        open(os.path.join(root, p), "w").write(text)
    subprocess.run(["git", "init", "-q", root], check=True)
    subprocess.run(["git", "-C", root, "add", "-A"], check=True)
    codemap.setup(root, os.path.join(root, ".progress"))
    g = codemap.build(quiet=True)
    eps, files = g["endpoints"], g["files"]

    for k in ("POST /api/orders", "GET /api/orders/:id", "GET /shop/v1/items", "POST /shop/v1/items", "DELETE /shop/v1/items/(?P<id>\\d+)", "GET /health"):
        assert k in eps, (k, sorted(eps))
    assert eps["POST /shop/v1/items"]["public"] and not eps["GET /shop/v1/items"]["public"], "per-method permission_callback"
    assert not eps["DELETE /shop/v1/items/(?P<id>\\d+)"]["checked"], "missing permission_callback"
    assert "server/billing.js" in files["server/app.js"]["uses"], files["server/app.js"]
    assert "app/models.py" in files["app/views.py"]["uses"], files["app/views.py"]
    assert {"POST /api/orders", "GET /api/orders/:id"} <= set(files["web/api.js"]["calls"]), files["web/api.js"]["calls"]
    assert g["auto"] and {p["id"] for p in g["parts"]} >= {"server", "web", "plugin", "app"}

    rules = {i["rule"] for i in g["health"]["issues"]}
    for r in ("secret", "env-in-git", "wp-no-permission", "public-write", "sql", "cors"):
        assert r in rules, (r, rules)
    assert len(next(i for i in g["health"]["issues"] if i["rule"] == "secret")["locations"]) == 2, "secret names like SENDGRID_API_KEY"
    sql = next(i for i in g["health"]["issues"] if i["rule"] == "sql")
    assert not any("DELETE FROM {$table}" in l["text"] for l in sql["locations"]), "table-name interpolation is not SQL injection"

    # Describe it, then change a file: the part is flagged for re-description.
    os.makedirs(os.path.join(root, ".progress", "map"), exist_ok=True)
    json.dump({"parts": [{"id": "api", "name": "API", "kind": "server", "paths": ["server/**", "plugin/**", "app/**"]},
                         {"id": "web", "name": "Web", "kind": "screen", "paths": ["web/**"]}],
               "links": []}, open(os.path.join(root, ".progress", "map", "architecture.json"), "w"))
    codemap.described()
    assert not codemap.build(quiet=True)["stale"]
    open(os.path.join(root, "web", "api.js"), "a").write("export function extra() {}\n")
    g = codemap.build(quiet=True)
    assert g["stale"] == ["web"], g["stale"]
    assert any(e["from"] == "web" and e["to"] == "api" and e["calls"] for e in g["edges"]), g["edges"]
    print("codemap ok")


if __name__ == "__main__":
    main()
