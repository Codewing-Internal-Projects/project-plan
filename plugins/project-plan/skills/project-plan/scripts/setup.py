#!/usr/bin/env python3
"""Install (or remove) the project-plan board in a project.

  setup.py [project_dir]               install, or refresh an existing install
  setup.py [project_dir] --uninstall   remove the hooks and CLAUDE.md rules (keeps .progress/ history)

Install: creates .progress/ with the page and a free port, git-ignores .progress/, and
starts the board server. As a plugin, that's all: the plugin's hooks do the tracking and
the session start hook gives Claude the rules. Installed by hand as a skill, it also merges
the hooks into .claude/settings.json and writes the rules into CLAUDE.md between markers.
"""
import json, os, socket, sys, time

SKILL = os.path.dirname(os.path.realpath(__file__))
MARK = "project-plan/scripts/hook.py"
COMMAND = 'python3 "$HOME/.claude/skills/project-plan/scripts/hook.py"'
EVENTS = {  # event -> matcher (None = no matcher)
    "SessionStart": None, "UserPromptSubmit": None, "PostToolUse": "*", "PostToolUseFailure": "*",
    "PermissionRequest": "*", "Notification": None, "Stop": None, "SessionEnd": None,
    "PreToolUse": "Agent|Task", "SubagentStart": None, "SubagentStop": None,
}
START, END = "<!-- project-plan:start -->", "<!-- project-plan:end -->"

RULES = """{start}
## Progress board

The user watches every Claude session in this project live at http://localhost:{port} (the `project-plan` skill), answers your proposals there, and reviews the history of every request and what was done for it. Hooks record your prompts, task list, tool calls, approval requests, failures and check results automatically.

- **Plan first.** Before you start on any request that needs tools, write the whole plan with the task list tool, so the user can see what is planned, what is being done and what is left. Give every task a short title and a one-line summary: with TodoWrite write each item as `Title — summary`; with TaskCreate use `subject` for the title and `description` for the summary.
- Keep it current: one task `in_progress` at a time, mark it completed as soon as it's done, and add or drop tasks when the plan changes. The board times each task, so don't batch status updates at the end.
- No task list tool in this session? Use `python3 ~/.claude/project-plan/hook.py plan "Title — summary" ...` to write the plan, `... add "Title — summary"` to add a task and `... task <n> in_progress|done|blocked` to update task n (1-based).
- Before saying work is done, make sure the latest test/check/build ran after your last edit: the board flags a passing check that is older than your edits.
- Never edit anything in `.progress/` by hand: several sessions and the board server write it under a lock. The one exception is `.progress/map/architecture.json`, the project map you write when asked.

### Project map

`.progress/map/` maps the code into parts (screens, server, data, outside services) with their files, endpoints and connections; the board shows it under Map. Before searching the code from scratch, try `python3 ~/.claude/project-plan/hook.py map find <words>`, `... map show <part|file|endpoint>` and `... map overview`. When the user asks to make or update the map, run `... map worksheet` and follow it. The free rescan runs by itself; describing parts is only on request.

### Proposals, board requests and the listener

The board is also how the user answers you. Keep `python3 ~/.claude/project-plan/hook.py listen` running in the background (Bash `run_in_background`). It exits when the user answers a proposal or sends a new request (brainstorm, suggest features, do a task) from the board, and prints what they sent: act on it, then start it again.

When the user asks you to brainstorm, suggest features, or choose between approaches, **post a proposal on the board instead of asking in chat**, then stop and wait for their answer. Build only after they approve it, following their answers and additional instructions. If they ask for changes, revise it with `--revise <ID>`.

```bash
python3 ~/.claude/project-plan/hook.py propose - <<'JSON'
{{"title": "Short name", "kind": "feature|brainstorm|plan|design",
 "summary": "One or two sentences: what this is and why.",
 "body": "Markdown: the idea, how it would work, trade-offs.",
 "questions": [
   {{"text": "Which layout?", "type": "one", "options": [{{"label": "Tabs", "detail": "why", "recommended": true}}, {{"label": "Sidebar"}}]}},
   {{"text": "Which of these should I build?", "type": "many", "options": [{{"label": "Idea A", "detail": "one line, effort"}}]}},
   {{"text": "Anything else I should know?", "type": "text"}}],
 "tasks": ["Title — summary", "Title — summary"],
 "preview": "/path/to/self-contained-mockup.html"}}
JSON
```
Add `"map": {{"parts": [{{"id": "billing", "name": "Billing", "kind": "server", "summary": "…", "status": "new|changed|existing", "files": ["…"]}}], "links": [{{"from": "billing", "to": "stripe", "label": "…"}}]}}` to a proposal that adds or reshapes parts of the app: the board draws it as a blueprint (reuse map part ids for existing parts). Everything but `title` is optional. Ask only questions whose answer changes what you build; mark your recommended option. Use `preview` (a self-contained HTML file) when a mockup shows the idea better than words. Track it with `hook.py proposal <ID> building`, then `done` when finished.
{end}"""


def free_port(start=4321):
    for port in range(start, start + 100):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise SystemExit("No free port between 4321 and 4420.")


def serving(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def load_settings(path):
    if not os.path.exists(path):
        return {}
    try:
        return json.load(open(path))
    except ValueError as e:
        raise SystemExit(f"{path} is not valid JSON ({e}). Fix it first; nothing was changed.")


def strip_ours(settings):
    """Remove only our hook entries, leaving everything else as it was."""
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        groups = []
        for g in hooks[event]:
            g = {**g, "hooks": [h for h in g.get("hooks", []) if MARK not in h.get("command", "")]}
            if g["hooks"]:
                groups.append(g)
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    if "hooks" in settings and not hooks:
        del settings["hooks"]
    return settings


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def set_rules(path, block):
    text = open(path).read() if os.path.exists(path) else ""
    if START in text and END in text:
        before, rest = text.split(START, 1)
        after = rest.split(END, 1)[1]
        text = before + block + after if block else (before.rstrip() + "\n\n" + after.lstrip()).strip() + "\n"
    elif block:
        text = (text.rstrip() + "\n\n" if text.strip() else "") + block + "\n"
    if text.strip():
        open(path, "w").write(text)
    elif os.path.exists(path):
        os.remove(path)  # we created it and it's empty again


def install(root):
    prog = os.path.join(root, ".progress")
    os.makedirs(prog, exist_ok=True)
    port_file = os.path.join(prog, "port")
    port = int(open(port_file).read().strip()) if os.path.exists(port_file) else free_port()
    open(port_file, "w").write(f"{port}\n")

    import hook  # same folder: copies the page and starts (or upgrades) the board server
    settings_path = os.path.join(root, ".claude", "settings.json")
    if hook.IS_PLUGIN:
        # The plugin's own hooks track every project with a .progress/ folder, and its session start hook
        # gives Claude the rules: nothing to write into the project. Remove what a hand install left behind.
        if os.path.exists(settings_path):
            before = load_settings(settings_path)
            after = strip_ours(json.loads(json.dumps(before)))
            if after != before:
                write_json(settings_path, after)
        set_rules(os.path.join(root, "CLAUDE.md"), "")
    else:
        settings = strip_ours(load_settings(settings_path))
        hooks = settings.setdefault("hooks", {})
        for event, matcher in EVENTS.items():
            group = {"hooks": [{"type": "command", "command": COMMAND}]}
            if matcher:
                group = {"matcher": matcher, **group}
            hooks.setdefault(event, []).append(group)
        write_json(settings_path, settings)
        set_rules(os.path.join(root, "CLAUDE.md"), RULES.format(start=START, end=END, port=port))

    if os.path.isdir(os.path.join(root, ".git")):
        gi = os.path.join(root, ".gitignore")
        text = open(gi).read() if os.path.exists(gi) else ""
        if not any(l.strip().rstrip("/") in (".progress", "/.progress") for l in text.splitlines()):
            open(gi, "a").write(("\n" if text and not text.endswith("\n") else "") + ".progress/\n")

    hook.use_project(root)
    hook.ensure_server()
    for _ in range(30):
        if serving(port):
            break
        time.sleep(0.1)
    print(f"Board installed in {root}\nOpen http://localhost:{port}\nAll your projects: http://localhost:{hook.HUB_PORT}")


def uninstall(root):
    settings_path = os.path.join(root, ".claude", "settings.json")
    if os.path.exists(settings_path):
        write_json(settings_path, strip_ours(load_settings(settings_path)))
    set_rules(os.path.join(root, "CLAUDE.md"), "")
    import hook
    hook.use_project(root)
    hook.register(False)  # off the all-projects hub
    print(f"Board hooks and CLAUDE.md rules removed from {root}. History kept in .progress/ (delete it if you don't need it).")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = os.path.abspath(args[0] if args else os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    if root == os.path.expanduser("~") or root == "/":
        raise SystemExit(f"Refusing to install in {root}: run it from a project folder.")
    (uninstall if "--uninstall" in sys.argv else install)(root)
