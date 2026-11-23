"""UI-independent session commands. Every public result is a JSON event."""

import json
import re
from datetime import datetime, timezone

from . import actions
from .memory import GlobalMemory, classify
from .context import FRAMING, SHARED, clip, memory_context, messages, prompt, size, task_messages
from .workspace import (CONFIG, INDEX, LESSONS, STATE, TODO, Workspace, digest,
                        packed, parse_title, render_tasks, rows, safe_name)


STEP_LIMIT = 8


def event(kind, **data):
    return {"event": kind, **data}


def fresh_task(title):
    return {"title": title, "reads": {}, "changed": {}, "fixes": [], "revision": 0,
            "verified": -1, "observation": "", "issue": None, "had_bug": False}


def notice_list(ws, state):
    notices = []
    if len(ws.read(LESSONS, "").splitlines()) > 30:
        notices.append("Run james consolidate to merge lessons and archive the originals.")
    for entry in state["workflows"].values():
        if entry["count"] > 2 and not entry.get("saved"):
            notices.append(f"Run james promote {entry['name']} to save its repeated workflow in tools/.")
    return notices


def one_line(text):
    return " ".join(text.split())


def bounded_observation(active, message):
    active["observation"] = clip(message, 7800)


class James:
    def __init__(self, root, model, report=None):
        self.ws = Workspace(root)
        self.model = model
        self.global_memory = GlobalMemory()
        self.report = report or (lambda message: None)

    def system_prompt(self, name):
        system = prompt(name)
        return system + memory_context(self.ws.read(INDEX, ""), self.ws.read(LESSONS, ""),
                                       self.global_memory.read("GLOBAL_LESSONS.md"),
                                       self.global_memory.read("GLOBAL_TOOLS.md"), SHARED - size(system))

    def ask(self, question):
        if not question.strip():
            raise ValueError("Supply a question.")
        answer = self.model.reply(messages(self.system_prompt("ask"), question), structured=False)
        return event("answer", text=answer)

    def status(self):
        s = self.ws.state()
        return event("state", phase=s["phase"], todo=self.ws.read(TODO, ""),
                     current=(s.get("active") or {}).get("title"), notices=notice_list(self.ws, s))

    def command(self, name, argument=""):
        if name == "ask":
            return self.ask(argument)
        if name == "status":
            return self.status()
        handlers = {"plan": self.plan, "approve": self.approve, "step": self.step,
                    "hint": self.hint, "confirm": self.confirm, "consolidate": self.consolidate,
                    "promote": self.promote, "packet": self.packet}
        with self.ws.writer():
            self.ws.init()
            if name == "init":
                return event("initialized", workspace=str(self.ws.root))
            return handlers[name](self.ws.state(), argument)

    def save(self, state, changes=None, expected=None):
        self.ws.commit({**(changes or {}), STATE: packed(state)}, expected)

    def plan(self, state, goal, archive=None):
        if (archive is None and state["phase"] not in {"idle", "complete", "review"}) or not goal.strip():
            raise ValueError("Supply a goal with no approved plan in progress.")
        before = digest(self.ws.path(TODO).read_bytes())
        context = "Goal\n" + goal + "\nWorkspace paths\n" + clip("\n".join(self.ws.inventory()), 4000)
        schema = {"type": "object", "properties": {"tasks": {"type": "array", "minItems": 5,
                  "maxItems": 15, "items": {"type": "string"}}}, "required": ["tasks"]}
        feedback = ""
        for attempt in range(2):
            try:
                answer = self.model.reply(messages(self.system_prompt("plan"), context + feedback), structured=schema)
                titles = answer.get("tasks")
                if not isinstance(titles, list):
                    raise ValueError("Planner must return a tasks array.")
                for title in titles:
                    if len(parse_title(title)) != 1:
                        raise ValueError("Use exactly one target file per task. Split multi-file tasks into separate rows.")
                todo = render_tasks([(False, title) for title in titles])
                rows(todo)
                break
            except ValueError as exc:
                if attempt == 1:
                    raise ValueError(f"Plan validation failed after 2 attempts (1 retry): {exc}") from exc
                feedback = f"\nPrevious draft rejected: {exc}. Return a complete corrected plan."
        state.pop("active", None)
        state.update(phase="review", approved=[], completed=[], goal=goal)
        self.save(state, {**(archive or {}), TODO: todo}, {TODO: before})
        return event("review_required", todo=todo)

    def approve(self, state, _):
        tasks = rows(self.ws.read(TODO))
        done = [t for checked, t in tasks if checked]
        titles = [t for _, t in tasks]
        if done != state["completed"] or titles[:len(done)] != done:
            raise ValueError("Only James checks off completed tasks. Preserve the completed prefix.")
        if len(done) == len(tasks):
            raise ValueError("All tasks are complete.")
        if state.get("active") and titles[len(done)] != state["active"]["title"]:
            raise ValueError("Keep the in-progress row intact when editing future tasks.")
        state["approved"] = titles
        if state["phase"] != "halted":
            state["phase"] = "ready"
        self.save(state)
        return event("approved", current=titles[len(done)])

    def approved_rows(self, state, text=None):
        tasks = rows(self.ws.read(TODO) if text is None else text)
        if tasks != [(title in state["completed"], title) for title in state["approved"]]:
            raise ValueError("TODO.md changed. Review it and run james approve.")
        return tasks

    def step(self, state, _):
        if state["phase"] == "halted":
            return self.packet(state, "")
        if state["phase"] != "ready":
            raise ValueError("Execution requires an approved, unfinished plan.")
        tasks = self.approved_rows(state)
        title = next(t for done, t in tasks if not done)
        active = state.setdefault("active", fresh_task(title))
        scope = parse_title(title)
        flows = actions.workflows(self.ws)
        if active.get("inflight"):
            return event("paused", reason="Previous command was interrupted. Inspect its effects and use james hint to resume.")
        active["read_requests"] = {}
        active["patched_files"] = {}
        self.save(state)
        rejected = 0
        for turn in range(1, STEP_LIMIT + 1):
            self.report(f"Action {turn}/{STEP_LIMIT} · Reading index and waiting for model")
            action = {}
            self.approved_rows(state)
            module_path = next((p for p in scope if p.casefold().startswith("memory/")), None)
            module = ""
            if module_path and self.ws.path(module_path).is_file():
                with self.ws.path(module_path).open("rb") as stream:
                    module = stream.read(1610).decode("utf-8-sig", "ignore")
            chat = task_messages(title, self.ws.read(INDEX), self.ws.read(LESSONS), module, active, flows,
                                 self.global_memory.read("GLOBAL_LESSONS.md"),
                                 self.global_memory.read("GLOBAL_TOOLS.md"),
                                 missing=[p for p in scope if not self.ws.path(p).exists()])
            try:
                active["budget"] = {"input_bound": sum(size(m["content"]) for m in chat) + FRAMING,
                                    "pages": [p for p, page in active.get("pages", {}).items()
                                              if page in chat[-1]["content"]], "module": module_path if module else None}
                self.save(state)
                action = self.model.reply(chat)
                if action.get("op") == "write_file":
                    action.update(op="create", text=action.get("text", action.get("content")))
                self.approved_rows(state)
                result = self.act(state, scope, flows, action)
                rejected = 0
            except (ValueError, FileNotFoundError, IsADirectoryError) as exc:
                state = self.ws.state()
                active = state["active"]
                self.report(f"Tool error · {exc}")
                bounded_observation(active, f"Action rejected: {exc}")
                if action.get("op") in {"create","read","patch"} and isinstance(action.get("path"), str):
                    try:
                        target = safe_name(action["path"], True)
                    except ValueError:
                        target = None
                    if target:
                        strikes = active.setdefault("tool_failures", {}).setdefault(target, [])
                        strikes.append({"fix": action["op"] + " " + target, "reason": str(exc)})
                        if len(strikes) >= 2:
                            active["tool_issue"] = {"source":"tool", "symptom":str(exc),
                                                    "diagnostic":str(exc), "attempts":strikes[-2:]}
                            active["tool_path"] = target if target in scope else scope[0]
                            state["phase"] = "halted"
                            self.save(state)
                            return self.packet(state, "")
                self.save(state)
                rejected += 1
                if rejected == 3:
                    return event("paused", reason=f"Three invalid model actions. Last feedback: {exc}")
                result = None
            if result:
                return result
        return event("paused", reason="Reached the 8-action limit. Review the feed, add a hint or run another step.")

    def act(self, state, scope, flows, action):
        active = state["active"]
        op = actions.field(action, "op", 16)
        path = action.get("path")
        if path is not None:
            path = safe_name(path, project_file=True)
        if op in {"read", "patch", "create", "symptom"} and path not in scope:
            raise ValueError("Choose a file from the current task's approved scope.")
        patched = active.setdefault("patched_files", {})
        verified = {target for target, stamp in patched.items() if active["reads"].get(target) == stamp}
        if op in {"read", "patch", "create"} and path in verified and not active["issue"]:
            if (op in {"read", "patch"} and verified.issuperset(scope)
                    and not (active["had_bug"] or active.get("hint"))
                    and (len(flows) <= 1 or active["verified"] == active["revision"])
                    and all(self.ws.path(target).is_file()
                            and digest(self.ws.path(target).read_bytes()) == patched[target] for target in scope)):
                self.report("All targets patched and read back · Finishing step")
                return self.finish(state, flows, {
                    "done": f"Patched and read back {scope[0]}." if len(scope) == 1
                            else f"Patched and read back all {len(scope)} target files.",
                    "spark": "Capture the changed behavior in a focused regression check.",
                })
            raise ValueError(f"{path} was already patched and verified in this step. "
                             "Finish when the remaining targets and checks are complete.")
        names = {"read":"Reading", "create":"Writing", "patch":"Patching", "run":"Running",
                 "symptom":"Checking behavior", "finish":"Verifying completion"}
        if op != "read":
            self.report(f"{names.get(op, op)} {path or action.get('name', '')}".strip())
        if op in {"read","patch"} and not self.ws.path(path).exists():
            if op == "patch" and action.get("old") == "" and isinstance(action.get("new"), str):
                op = "create"
                action = {**action, "op":op, "text":action["new"]}
                self.report(f"Writing new file {path}")
            else:
                raise ValueError(f"{path} does not exist. Use create with path and text containing the new file.")
        changes, expected = {}, {}
        if op == "read":
            start, count = action.get("start", 1), action.get("lines", 45)
            if type(start) != int or start < 1 or type(count) != int or not 1 <= count <= 100:
                raise ValueError("Use a positive start line and 1–100 lines per page.")
            pages = active.setdefault("pages", {})
            seen = active.setdefault("read_requests", {}).setdefault(path, {})
            request = f"{start}:{count}"
            stat = self.ws.path(path).stat()
            stamp = [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino]
            previous = seen.get(request)
            if previous is not None and previous["stamp"] == stamp:
                if not active["changed"]:
                    if (not active["issue"] and not (active["had_bug"] or active.get("hint"))
                            and (len(flows) <= 1 or active["verified"] == active["revision"])
                            and all(active["read_requests"].get(target)
                                    and self.ws.path(target).is_file()
                                    and active["reads"].get(target) == digest(self.ws.path(target).read_bytes())
                                    for target in scope)):
                        self.report("Existing targets inspected · Finishing step")
                        return self.finish(state, flows, {
                            "done": "The existing target files already satisfy the task.",
                            "spark": "Keep a focused regression check for the existing behavior.",
                        })
                    result = "Already read in this step. Finish after inspecting remaining targets and resolving outstanding checks."
                    self.report(result)
                    bounded_observation(active, result)
                    self.save(state)
                    return None
                raise ValueError("Already read in this step — content is already in your context. "
                                 "Now either patch the file or call finish.")
            self.report(f"Reading {path}")
            source, fingerprint = actions.read_source(self.ws, path)
            page = f"{path} from line {start}\n" + actions.lines_at(source, start, count)
            seen[request] = {"stamp": stamp}
            pages.pop(path, None)
            pages[path] = clip(page, 7000)
            active["reads"][path] = fingerprint
            result = f"Read {path} from line {start}."
        elif op in {"patch", "create"}:
            changes, expected = actions.edit(self.ws, active, action, path)
            active.get("tool_failures", {}).pop(path, None)
            if changes:
                active.get("pages", {}).pop(path, None)
                active.get("read_requests", {}).pop(path, None)
                if op == "patch":
                    patched[path] = digest(changes[path].encode("utf-8"))
                else:
                    patched.pop(path, None)
            result = (active["fixes"][-1] if changes else
                      f"{path} already matches the supplied text. Return finish now if the task goal is met.")
        elif op == "run":
            name = actions.field(action, "name", 50)
            if name not in flows:
                raise ValueError("Choose a configured workflow.")
            active["inflight"] = name
            self.save(state)
            code, output, diagnosis = actions.execute(self.ws.root, flows[name])
            del active["inflight"]
            active["checked_revision"] = active["revision"]
            if len(flows[name]) > 1:
                key = actions.workflow_id(flows[name])
                entry = state["workflows"].setdefault(key, {"name": name, "count": 0})
                entry.update(name=name, count=entry["count"] + 1)
            if code:
                return self.failure(state, name, output, actions.signature(diagnosis))
            if active["issue"] and active["issue"]["source"] == name:
                active["issue"] = None
            active["verified"] = active["revision"]
            result = f"{name} passed\n" + output[-4200:]
        elif op == "symptom":
            line = action.get("line", 1)
            if type(line) != int or line < 1:
                raise ValueError("Supply a positive source line.")
            active["location"] = [path, line]
            symptom = actions.field(action, "message", 8000)
            return self.failure(state, "behavior", symptom, actions.signature(symptom))
        elif op == "finish":
            return self.finish(state, flows, action)
        else:
            raise ValueError(f"Unknown action {op}.")
        bounded_observation(active, result)
        self.save(state, changes, expected)
        return None

    def failure(self, state, source, diagnostic, symptom):
        self.report(f"Tool error · {source}: {symptom}")
        a = state["active"]
        issue = a["issue"]
        same_issue = bool(issue and (issue["source"], issue["symptom"]) == (source, symptom))
        guided = a.pop("guided_fix", False)
        had_baseline = same_issue or (not issue and guided)
        if not same_issue:
            issue = {"source": source, "symptom": symptom, "attempts": [], "revision": -1}
        # A baseline failure or repeat observation without any fix is not an attempt.
        if had_baseline and a["fixes"] and a["revision"] > issue["revision"]:
            issue["attempts"].append({"fix": " / ".join(a["fixes"]), "reason": symptom})
        issue.update(diagnostic=diagnostic, revision=a["revision"])
        a.update(issue=issue, had_bug=True, fixes=[])
        bounded_observation(a, f"{source} failed: {symptom}\n" + diagnostic[-2400:])
        if len(issue["attempts"]) >= 2:
            state["phase"] = "halted"
        self.save(state)
        if state["phase"] == "halted":
            return self.packet(state, "")
        return None

    def packet(self, state, _):
        if state["phase"] != "halted":
            raise ValueError("A consultation packet requires two consecutive failed attempts.")
        a = state["active"]
        issue = a.get("tool_issue") or a["issue"]
        location = [a["tool_path"], 1] if a.get("tool_issue") else a.get("location") if issue["source"] == "behavior" else None
        path, line = location or a.get("last_edit", [parse_title(a["title"])[0], 1])
        # Prefer the deepest in-scope traceback frame over the last edited location.
        for frame, number in re.findall(r'File "([^"]+)", line (\d+)', issue["diagnostic"]):
            for target in parse_title(a["title"]):
                if frame.replace("\\", "/").endswith("/" + target) or frame == target:
                    path, line = target, int(number)
        try:
            source = self.ws.read(path, "")
        except (ValueError, OSError):
            source = ""
        start = max(1, min(line - 15, len(source.splitlines()) - 39))
        snippet = actions.lines_at(source, start, 40, numbered=True)
        attempts = "\n".join(f"{i}. {t['fix']} — {t['reason']}" for i, t in enumerate(issue["attempts"], 1))
        missing = (f"Attach the expected result and smallest failing input for {path}."
                   if snippet else f"Attach the current contents of {path} and the smallest failing input.")
        body = (f"# James Cloud Consultation Packet\n\n## Current Goal\n{a['title']}\n\n"
                f"## Error / Symptom\n```text\n{issue['diagnostic']}\n```\n\n"
                f"## What I Already Tried (2 Attempts)\n{attempts}\n\n"
                f"## Relevant Code Context\n{path}, from line {start}\n```\n{snippet}\n```\n\n"
                f"## Missing Info Needed\n{missing}")
        return event("consultation", packet=body)

    def hint(self, state, hint):
        a = state.get("active")
        if not a or not hint.strip() or len(hint.encode()) > 6000:
            raise ValueError("Supply a hint for the current task (up to 6,000 UTF-8 bytes).")
        a["hint"] = hint
        a["guided_fix"] = True
        a.pop("inflight", None)
        if a.pop("tool_issue", None):
            a.get("tool_failures", {}).pop(a.pop("tool_path"), None)
        elif state["phase"] == "halted":
            a["issue"]["attempts"] = []
        state["phase"] = "ready"
        self.save(state)
        return event("hint_saved", current=a["title"])

    def confirm(self, state, _):
        a = state.get("active")
        if not a or a.get("tool_issue") or not a["issue"] or a["issue"]["source"] != "behavior":
            raise ValueError("Confirm is for a manually verified behavioral fix.")
        a.update(issue=None, verified=a["revision"])
        state["phase"] = "ready"
        self.save(state)
        return event("confirmed", current=a["title"])

    def finish(self, state, flows, action):
        a = state["active"]
        if a["issue"]:
            if a["issue"]["source"] == "behavior":
                return event("paused", reason="Inspect the reported behavior. Use james confirm if resolved, or james hint with your findings.")
            raise ValueError("Rerun the failing workflow successfully before finishing.")
        if flows and a["verified"] != a["revision"]:
            if len(flows) != 1:
                raise ValueError("Run a relevant verification workflow after the latest edit.")
            result = self.act(state, parse_title(a["title"]), flows, {"op": "run", "name": next(iter(flows))})
            if result or a["issue"]:
                return result
        if not a["reads"] and not a["changed"] and a["verified"] < 0:
            raise ValueError("Inspect a scoped artifact or verify the task before finishing.")
        if not flows and any(a["reads"].get(path) != digest(self.ws.path(path).read_bytes())
                             for path in a["changed"]):
            raise ValueError("Read back every changed artifact before completion.")
        done = one_line(actions.field(action, "done", 320))
        if re.fullmatch(r"(?:the |this )?(?:current )?task (?:has been |is |was )?"
                        r"(?:successfully )?complete[d]?(?: successfully)?[.!]?|done[.!]?",
                        done, re.IGNORECASE):
            done = f"Completed “{a['title'].partition(' | ')[0]}”."
        spark = one_line(actions.field(action, "spark", 200))
        lesson = action.get("lesson", "")
        if a["had_bug"] or a.get("hint") or lesson:
            lesson = one_line(actions.field(action, "lesson", 260))
        todo_text, todo_hash = self.ws.snapshot(TODO)
        tasks = self.approved_rows(state, todo_text)
        index_text, index_hash = self.ws.snapshot(INDEX)
        expected = {TODO: todo_hash, INDEX: index_hash}
        index = index_text.splitlines()
        for path, symbols in a["changed"].items():
            prefix = f"- `{path}`"
            prior = next((i for i, s in enumerate(index) if s.split(" — ")[0] == prefix), None)
            if path.endswith(".py"):
                api = actions.exported_symbols(self.ws.read(path))
                if api is not None:
                    symbols = api
            elif prior is not None:
                _, _, previous = index[prior].partition(" — ")
                source = self.ws.read(path)
                surviving = [s for s in previous.split(", ") if s and s in source]
                symbols = list(dict.fromkeys(surviving + symbols))
            row = prefix + (" — " + ", ".join(symbols) if symbols else "")
            if prior is None:
                index.append(row)
            else:
                index[prior] = row
        changes = {TODO: render_tasks([(c or t == a["title"], t) for c, t in tasks]),
                   INDEX: "\n".join(index) + "\n"}
        if lesson:
            local, shared = classify(self.model, [lesson], a["title"])
            self.global_memory.add(shared)
            if local:
                original, expected[LESSONS] = self.ws.snapshot(LESSONS)
                changes[LESSONS] = original.rstrip() + f"\n- {lesson}\n"
        state["completed"].append(a["title"])
        state["phase"] = "complete" if len(state["completed"]) == len(tasks) else "ready"
        del state["active"]
        self.save(state, changes, expected)
        return event("completed", done=done, changed=a["changed"], spark=spark,
                     notices=notice_list(self.ws, state))

    def consolidate(self, state, _):
        original, before = self.ws.snapshot(LESSONS)
        rules = [s for s in original.splitlines() if s.strip() and not s.startswith("#")]
        if not rules:
            return event("unchanged", reason="No lessons to consolidate.")
        # Process bounded batches even when a user imports a long lesson file.
        batches, group = [], ""
        for rule in rules:
            if len((group + rule).encode()) > 8000 and group:
                batches.append(group)
                group = ""
            group += rule + "\n"
        local, shared = [], []
        for batch in batches + [group]:
            values = self.model.reply(messages(prompt("consolidate"), batch)).get("rules")
            if not isinstance(values, list) or not values or not all(
                    isinstance(v, str) and v.strip() and len(v) <= 300 for v in values):
                raise ValueError("Consolidation returned invalid rules.")
            project_rules, global_rules = classify(
                self.model, [one_line(v) for v in values], "Consolidate lessons from this project's memory.")
            local.extend(project_rules)
            shared.extend(global_rules)
        unique = {r.casefold(): r for r in local}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archive = f"memory/archive/LESSONS-{stamp}.md"
        changes = {archive: original, LESSONS: "# Lessons\n" + "".join(f"- {r}\n" for r in unique.values())}
        self.global_memory.add(shared)
        self.ws.commit(changes, {LESSONS: before, archive: None})
        return event("consolidated", archive=archive, rules=len(unique))

    def promote(self, state, name):
        flows = actions.workflows(self.ws)
        if name not in flows:
            raise ValueError("Choose a configured workflow.")
        entry = state["workflows"].get(actions.workflow_id(flows[name]), {})
        if entry.get("count", 0) <= 2:
            raise ValueError("This workflow has fewer than three recorded uses.")
        path = f"tools/{name}.py"
        if self.ws.path(path).exists():
            raise ValueError(f"{path} already exists.")
        script = ("import subprocess\nimport sys\nfrom pathlib import Path\n\n"
                  f"STEPS = {flows[name]!r}\n\n"
                  "if __name__ == '__main__':\n"
                  "    for step in STEPS:\n"
                  "        argv = [sys.executable if arg == '{python}' else arg for arg in step]\n"
                  "        subprocess.run(argv, cwd=Path(__file__).resolve().parents[1], check=True, timeout=90)\n")
        flows[name] = [["{python}", path]]
        entry["saved"] = True
        self.save(state, {path: script, CONFIG: packed(flows),
                          INDEX: self.ws.read(INDEX).rstrip() + f"\n- `{path}` — python {path}\n"})
        return event("promoted", path=path)
