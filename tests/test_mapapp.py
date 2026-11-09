"""Self-check for blueprints, notes, flows parsing and export: python3 tests/test_mapapp.py (exits non-zero on failure)."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugins", "project-plan", "skills", "project-plan", "scripts"))
import codemap  # noqa: E402
import mapapp  # noqa: E402

PLAN = {
    "source": "docs/prd.md", "summary": "A shop.", "stack": ["Next.js", "Postgres"], "assumptions": ["Card payments only"],
    "questions": ["Do we ship abroad?"],
    "parts": [
        {"id": "checkout", "name": "Checkout", "kind": "screen", "summary": "Cart and payment.", "files": [{"path": "app/checkout/page.tsx", "desc": "Checkout page"}]},
        {"id": "orders", "name": "Orders", "kind": "server", "summary": "Order API.", "files": [{"path": "app/api/orders/route.ts", "desc": "Orders"}]},
        {"id": "stripe", "name": "Stripe", "kind": "service", "summary": "Payments."},
        {"id": "reports", "name": "Reports", "kind": "server", "summary": "Not wired yet."},
    ],
    "endpoints": [
        {"method": "POST", "path": "/api/orders", "part": "orders", "file": "app/api/orders/route.ts", "desc": "Create", "auth": "public", "changes_data": True},
        {"method": "GET", "path": "/api/orders/{id}", "part": "orders", "file": "app/api/orders/route.ts", "desc": "Read", "auth": "undecided"},
    ],
    "screens": [{"id": "checkout", "name": "Checkout", "route": "/checkout", "part": "checkout", "calls": ["POST /api/orders"]}],
    "links": [{"from": "orders", "to": "stripe", "label": "PaymentIntent", "kind": "uses"}],
    "flows": [{"id": "buy", "name": "Buying", "steps": [{"node": "screen:checkout", "text": "Pay"}]}],
}


def main():
    root = tempfile.mkdtemp()
    codemap.setup(root, os.path.join(root, ".progress"))
    os.makedirs(codemap.MAP)

    assert mapapp.check_plan({"parts": []}), "an empty plan is rejected"
    bad = json.loads(json.dumps(PLAN))
    bad["screens"][0]["calls"] = ["POST /api/nope"]
    assert any("isn't in endpoints" in p for p in mapapp.check_plan(bad))

    mapapp.wj(mapapp.mp("plan.json"), PLAN)
    assert mapapp.blueprint_mode()
    g = mapapp.planned(quiet=True)
    assert g["blueprint"] and len(g["parts"]) == 4 and "POST /api/orders" in g["endpoints"]
    assert any(e["from"] == "checkout" and e["to"] == "orders" for e in g["edges"]), g["edges"]
    rules = {i["rule"] for i in g["review"]}
    assert {"open-write", "who-may", "question", "orphan"} <= rules, rules
    assert g["plan"]["stack"] == ["Next.js", "Postgres"] and g["flows"][0]["id"] == "buy"

    # Undo restores the previous plan.
    changed = json.loads(json.dumps(PLAN))
    changed["parts"].append({"id": "refunds", "name": "Refunds", "kind": "server", "summary": "Refunds."})
    import shutil
    shutil.copyfile(mapapp.mp("plan.json"), mapapp.mp("plan.prev.json"))
    mapapp.wj(mapapp.mp("plan.json"), changed)
    assert len(mapapp.planned(quiet=True)["parts"]) == 5
    mapapp.undo()
    assert len(mapapp.load()["parts"]) == 4

    # Notes, answers from the first version, and reply parsing.
    mapapp.note("app/checkout/page.tsx", "Uses the new cart")
    assert mapapp.rj(mapapp.mp("notes.json"), {})["app/checkout/page.tsx"]["text"] == "Uses the new cart"
    mapapp.note("app/checkout/page.tsx", "")
    assert mapapp.rj(mapapp.mp("notes.json"), {}) == {}
    mapapp.wj(mapapp.mp("asks.json"), [{"q": "old", "a": "text", "parts": ["orders"], "cost": 0.1, "at": "x"}])
    assert mapapp.asks()[0]["nodes"][0]["id"] == "part:orders"
    assert mapapp.json_block('Sure.\n```json\n{"answer": "x", "nodes": []}\n```') == {"answer": "x", "nodes": []}
    assert mapapp.json_block('{"a": 1}') == {"a": 1}

    # Export: one file, no folder path, no health unless asked.
    if os.path.exists(os.path.join(mapapp.SKILL, "map.html")):
        html = mapapp.export()
        assert "window.MAPDATA" in html and root not in html
    print("mapapp ok")


if __name__ == "__main__":
    main()
