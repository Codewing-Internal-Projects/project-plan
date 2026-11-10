"""Security-audit runs for the board's Security view and the map's health link.

Cloudflare's security-audit skill writes each run to ~/security-audit-skill/<repo>/run-N/ (run-metadata.json, findings.json,
coverage-ledger.json and a few .md reports). This reads the runs whose target is this project; it never writes there.
"""
import glob
import json
import os
from datetime import datetime, timezone

AUDITS = os.path.expanduser("~/security-audit-skill")  # where the skill writes its runs by default
SKILL = ["~/.claude/skills/security-audit/SKILL.md", "~/.agents/skills/security-audit/SKILL.md",  # by hand, or `npx skills add`
         "~/.claude/plugins/cache/*/*/*/skills/security-audit/SKILL.md", "{root}/.claude/skills/security-audit/SKILL.md"]
SEV = ("critical", "high", "medium", "low", "informational")
FILE_CAP = 400_000  # bytes of each report file sent to the board


def _rj(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _obj(x):
    return x if isinstance(x, dict) else {}


def _list(x):
    return x if isinstance(x, list) else []


def skill_installed(root):
    """Whether Claude Code can load the security-audit skill here (globally, from a plugin, or in this project)."""
    r = glob.escape(os.path.abspath(root))
    return any(glob.glob(os.path.expanduser(p.format(root=r))) for p in SKILL)


def runs(root):
    """This project's finished runs, newest first: [{id, dir, target, when, meta}]."""
    root, out = os.path.abspath(root), []
    for meta in glob.glob(os.path.join(AUDITS, "*", "run-*", "run-metadata.json")):
        m, d = _obj(_rj(meta, {})), os.path.dirname(meta)
        target = os.path.abspath(str(m.get("target") or ""))
        found = os.path.join(d, "findings.json")
        if (target == root or target.startswith(root + os.sep)) and m.get("run_status") != "in_progress" and os.path.isfile(found):
            out.append({"id": os.path.relpath(d, AUDITS), "dir": d, "target": target, "meta": m, "mtime": os.path.getmtime(found),
                        "when": datetime.fromtimestamp(os.path.getmtime(found), timezone.utc).isoformat(timespec="seconds")})
    return sorted(out, key=lambda r: r["mtime"], reverse=True)


def _records(run):
    return [r for r in _list(_rj(os.path.join(run["dir"], "findings.json"), [])) if isinstance(r, dict)]


def _key(r):
    return str(r.get("fingerprint") or r.get("title") or "")[:200]


def summary(root):
    """The newest run in a line, for the map's health check: {when, profile, confirmed, needs} or None."""
    rs = runs(root)
    if not rs:
        return None
    recs = _records(rs[0])
    return {"when": rs[0]["when"], "profile": str(rs[0]["meta"].get("profile") or ""),
            "confirmed": sum(1 for r in recs if r.get("verdict") == "confirmed"),
            "needs": sum(1 for r in recs if r.get("verdict") == "needs_validation")}


def _loc(t, run, root):
    t = _obj(t)
    f = str(t.get("file") or "")
    if not f:
        return None
    rel = os.path.relpath(f if os.path.isabs(f) else os.path.join(run["target"], f), root)
    return {"file": "" if rel.startswith("..") else rel, "path": f, "line": t["line"] if isinstance(t.get("line"), int) else 0,
            "kind": str(t.get("kind") or ""), "scope": str(t.get("scope") or ""), "text": str(t.get("description") or "")}


def _finding(r, run, root):
    sev, rem, plan = _obj(r.get("severity")), _obj(r.get("remediation")), _obj(r.get("validation_plan"))
    level = str(sev.get("overall_severity") or "").lower()
    return {"id": "audit:" + _key(r)[:120], "key": _key(r), "verdict": str(r.get("verdict") or ""),
            "severity": level if level in SEV else "", "title": str(r.get("title") or "Security finding"),
            "description": str(r.get("description") or ""), "root_cause": str(r.get("claimed_root_cause") or r.get("root_cause") or ""),
            "trace": [x for x in (_loc(t, run, root) for t in _list(r.get("trace"))) if x],
            "evidence": [x for x in (_loc(t, run, root) for t in _list(r.get("evidence"))) if x],
            "blockers": [str(b) for b in _list(r.get("blockers"))],
            "check_here": str(plan.get("local") or ""), "check_live": str(plan.get("deployment") or ""),
            "fix": str(rem.get("strategy") or (r.get("remediation") if isinstance(r.get("remediation"), str) else "") or "")}


def _files(run):
    out = []
    for p in sorted(glob.glob(os.path.join(run["dir"], "*.md"))) + [os.path.join(run["dir"], n) for n in ("findings.json", "coverage-ledger.json")]:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                text = f.read(FILE_CAP + 1)
        except OSError:
            continue
        name = os.path.basename(p)
        if name.endswith(".json") and len(text) <= FILE_CAP:
            try:
                text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
            except ValueError:
                pass
        out.append({"name": name, "text": text[:FILE_CAP], "cut": len(text) > FILE_CAP})
    out.sort(key=lambda f: (f["name"] != "REPORT.md", f["name"].endswith(".json"), f["name"]))
    return out


def view(root, map_dir, run_id=""):
    """Everything the Security view shows for one run (the newest unless run_id names another), compared with the run before it."""
    root = os.path.abspath(root)
    rs = runs(root)
    out = {"skill": skill_installed(root), "runs": [{"id": r["id"], "when": r["when"], "profile": str(r["meta"].get("profile") or "")} for r in rs]}
    if not rs:
        return out
    i = next((n for n, r in enumerate(rs) if r["id"] == run_id), 0)  # only ids from the list: nothing outside the audits folder
    run, prev = rs[i], rs[i + 1] if i + 1 < len(rs) else None
    m = run["meta"]
    ref = _obj(m.get("source_ref"))
    dismissed = _obj(_rj(os.path.join(map_dir, "dismissed.json"), {}))
    findings = [_finding(r, run, root) for r in _records(run)]
    before = {_key(r): r for r in _records(prev)} if prev else {}
    for f in findings:
        d = dismissed.get(f["id"])
        f["dismissed"], f["reason"] = bool(d), _obj(d).get("reason", "")
        f["change"] = "" if not prev else "new" if f["key"] not in before else \
            "changed" if str(before[f["key"]].get("verdict") or "") != f["verdict"] else "same"
    now_keys = {f["key"] for f in findings}
    gone = [{"title": str(r.get("title") or ""), "verdict": str(r.get("verdict") or "")} for k, r in before.items() if k not in now_keys]
    units = [_obj(u) for u in _list(_rj(os.path.join(run["dir"], "coverage-ledger.json"), []))]
    out.update({
        "run": {"id": run["id"], "dir": run["dir"], "when": run["when"], "profile": str(m.get("profile") or ""), "status": str(m.get("run_status") or ""),
                "commit": str(ref.get("commit") or (m.get("source_ref") if isinstance(m.get("source_ref"), str) else "") or "")[:40],
                "dirty": bool(ref.get("worktree_dirty")), "sandbox": str(m.get("sandbox_capability") or ""), "prev": prev["id"] if prev else ""},
        "findings": findings, "gone": gone,
        "coverage": [{"surface": str(u.get("surface") or ""), "attack": str(u.get("attack_class") or ""), "status": str(u.get("status") or ""),
                      "paths": [str(p) for p in _list(u.get("reviewed_paths")) or _list(u.get("starting_paths"))][:12],
                      "unresolved": [str(x) if not isinstance(x, dict) else str(x.get("reason") or x.get("description") or x) for x in _list(u.get("unresolved"))][:5]}
                     for u in units],
        "files": _files(run)})
    return out
