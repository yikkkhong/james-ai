import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from james.context import FRAMING, OUTPUT, SHARED, WINDOW, messages, size, task_messages
from james.engine import James, fresh_task
from james.model import LocalModel
from james.workspace import CONFIG, INDEX, LESSONS, STATE, TODO, Workspace, digest, packed, safe_name

TITLES = ["Implement a counter | counter.py, memory/counter.md",
          "Add a formatter | formatter.py", "Add examples | examples.py",
          "Add a runner | run.py", "Write usage notes | README.md"]
FINISH = {"op": "finish", "done": "Added the counter.", "spark": "Exercise the zero case."}
READ = {"op": "read", "path": "counter.py"}


def change(old, new):
    return {"op": "patch", "path": "counter.py", "old": old, "new": new}


class Scripted:
    def __init__(self):
        self.actions = []
        self.chats = []

    def reply(self, chat, structured=True):
        self.chats.append(chat)
        if chat[0]["content"].startswith("Classify each supplied lesson"):
            if self.actions and "scopes" in self.actions[0]:
                return self.actions.pop(0)
            return {"scopes": ["project"] * len(json.loads(chat[1]["content"])["rules"])}
        if not self.actions:
            raise AssertionError("Unexpected model call")
        return self.actions.pop(0)


class Sessions(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"JAMES_HOME": str(self.root / "installation")})
        env.start()
        self.addCleanup(env.stop)
        self.model = Scripted()
        self.james = James(self.root, self.model)

    def approved(self):
        self.model.actions.append({"tasks": TITLES})
        self.james.command("plan", "Build a counter.")
        self.james.command("approve")

    def test_global_lesson_carries_to_another_workspace(self):
        self.approved()
        self.model.actions.extend([{"op": "create", "path": "counter.py", "text": "counter = 0\n"},
                                   READ, {**FINISH, "lesson": "Use UTF-8 for text files."},
                                   {"scopes": ["global"]}])
        self.james.command("step")
        self.assertNotIn("UTF-8", (self.root / LESSONS).read_text())
        self.assertIn("UTF-8", self.james.global_memory.read("GLOBAL_LESSONS.md"))
        self.james.global_memory.add(["Use UTF-8 for text files."])
        self.assertEqual(self.james.global_memory.read("GLOBAL_LESSONS.md").count("UTF-8"), 1)
        (self.james.global_memory.root / "GLOBAL_TOOLS.md").write_text("- tools/check.py verifies UTF-8\n")
        other = self.root / "other"
        other.mkdir()
        model = Scripted()
        model.actions.append("Understood.")
        James(other, model).command("ask", "What conventions apply?")
        self.assertIn("Use UTF-8", str(model.chats))
        self.assertIn("tools/check.py", str(model.chats))
        self.assertNotIn("counter.py", str(model.chats))
        self.assertEqual(list(other.iterdir()), [])

    def test_consolidation_routes_general_rules(self):
        self.james.command("init")
        (self.root / LESSONS).write_text("# Lessons\n- Use UTF-8.\n- Counter starts at three.\n")
        self.model.actions.extend([{"rules": ["Use UTF-8.", "Counter starts at three."]},
                                   {"scopes": ["global", "project"]}])
        self.james.command("consolidate")
        self.assertIn("Counter starts", (self.root / LESSONS).read_text())
        self.assertNotIn("UTF-8", (self.root / LESSONS).read_text())
        self.assertIn("UTF-8", self.james.global_memory.read("GLOBAL_LESSONS.md"))

    def test_ask_is_read_only(self):
        self.model.actions.append("Yes.")
        self.assertEqual(self.james.command("ask", "Can we brainstorm?")["text"], "Yes.")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_prefixed_plan_and_action_paths(self):
        titles = ["Create greeting | /path/to/workspace/./greetings.py", *TITLES[1:]]
        self.model.actions.extend([{"tasks": titles},
                                   {"op": "create", "path": ".\\greetings.py", "text": "hello = 1\n"},
                                   {"op": "read", "path": "./greetings.py"}, FINISH])
        self.james.command("plan", "Build greetings")
        self.james.command("approve")
        self.assertEqual(self.james.command("step")["changed"], {"greetings.py": []})
        for path in ("./../outside", ".\\C:/outside", ".//absolute", "./.git/config",
                     "/path/to/workspace/../outside", "/path/to/workspace//absolute",
                     "/path/to/workspace/.git/config", "/actual/workspace/greetings.py"):
            with self.assertRaises(ValueError):
                safe_name(path, project_file=True)

    def test_plan_retry_cap_preserves_previous_plan(self):
        self.james.command("init")
        original = (self.root / TODO).read_bytes()
        self.model.actions.extend([{"tasks": []}, {"tasks": []}, {"tasks": TITLES}])
        with self.assertRaisesRegex(ValueError, "after 2 attempts"):
            self.james.command("plan", "Build greetings")
        self.assertEqual(len(self.model.chats), 2)
        self.assertEqual((self.root / TODO).read_bytes(), original)
        self.assertEqual(self.james.ws.state()["phase"], "idle")

    def test_plan_recovers_on_single_retry(self):
        self.model.actions.extend([{"tasks": []}, {"tasks": TITLES}])
        self.assertEqual(self.james.command("plan", "Build greetings")["event"], "review_required")
        self.assertEqual(len(self.model.chats), 2)
        self.assertIn("Previous draft rejected", self.model.chats[-1][1]["content"])

    def test_gate_edited_plan_scope_and_fresh_context(self):
        self.model.actions.append({"tasks": TITLES})
        result = self.james.command("plan", "Build")
        self.assertEqual(result["event"], "review_required")
        with self.assertRaisesRegex(ValueError, "approved"):
            self.james.command("step")
        todo = self.root / TODO
        todo.write_text(todo.read_text().replace("Add a runner", "Create the runner"))
        self.james.command("approve")
        todo.write_text(todo.read_text().replace("Add examples", "Provide examples"))
        with self.assertRaisesRegex(ValueError, "changed"):
            self.james.command("step")
        self.james.command("approve")
        (self.root / "memory/counter.md").write_text("MODULE_ONE_ONLY")
        (self.root / "formatter.py").write_text("# FUTURE_PRIVATE_BODY")
        self.model.actions.extend([{"op": "create", "path": "formatter.py", "text": "bad"},
                                   {"op": "create", "path": "counter.py", "text": "def counter():\n    return 0\n"},
                                   READ, FINISH])
        done = self.james.command("step")
        self.assertEqual(done["changed"], {"counter.py": ["counter"]})
        self.assertIn("MODULE_ONE_ONLY", str(self.model.chats[1]))
        self.assertNotIn("Add a formatter", str(self.model.chats[1]))
        self.assertNotIn("FUTURE_PRIVATE_BODY", str(self.model.chats))
        self.assertEqual(todo.read_text().count("[x]"), 1)
        self.assertNotIn("active", self.james.ws.state())
        self.model.actions.extend([{"op": "read", "path": "formatter.py"}, FINISH])
        self.james.command("step")
        self.assertNotIn("MODULE_ONE_ONLY", str(self.model.chats[-1]))
        self.assertNotIn("Action rejected", str(self.model.chats[-1]))
        self.assertIn("counter.py", (self.root / INDEX).read_text(encoding="utf-8"))

    def test_breaker_full_diagnostics_resume_lesson_and_workflow(self):
        self.approved()
        flow = [["{python}", "-c", "print('diagnostic-start')"],
                ["{python}", "-c", "from counter import counter; assert counter() == 3, 'wrong count'"]]
        (self.root / CONFIG).write_text(json.dumps({"verify": flow}))
        source = "# context\n" * 24 + "def counter():\n    return 0\n" + "# footer\n" * 22
        (self.root / "counter.py").write_bytes(source.replace("\n", "\r\n").encode())
        run = {"op": "run", "name": "verify"}
        # Disable bytecode caching for this fast-changing fixture.
        with patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"}):
            self.model.actions.extend([run, READ, change("return 0", "return 1"), run,
                                       READ, change("return 1", "return 2"), run])
            packet = self.james.command("step")["packet"]
            self.assertIn("diagnostic-start", packet)
            self.assertIn("Traceback (most recent call last)", packet)
            self.assertIn("return 1", packet)
            self.assertIn("return 2", packet)
            self.assertIn("Missing Info Needed", packet)
            self.assertGreaterEqual(len([s for s in packet.splitlines() if ": #" in s]), 20)
            resumed = James(self.root, self.model)
            self.assertEqual(resumed.command("step")["packet"], packet)
            resumed.command("hint", "The expected result is three.")
            self.model.actions.extend([READ, change("return 2", "return 3"), run,
                                       {**FINISH, "lesson": "Check the counter against the specified result."}])
            result = resumed.command("step")
        self.assertTrue(result["notices"])
        self.assertIn("specified result", (self.root / LESSONS).read_text())
        self.assertNotIn("Traceback", (self.root / STATE).read_text(encoding="utf-8"))
        self.assertIn(b"return 3\r\n", (self.root / "counter.py").read_bytes())
        resumed.command("promote", "verify")
        self.assertTrue((self.root / "tools/verify.py").exists())
        self.assertEqual(json.loads((self.root / CONFIG).read_bytes())["verify"], [["{python}", "tools/verify.py"]])

    def test_whole_file_patch_and_stale_read(self):
        self.approved()
        (self.root / "counter.py").write_text("a=1\nb=2\n")
        self.model.actions.extend([READ, change("a=1\nb=2", "replaced"), change("missing", "oops"),
                                   change("a=1", "a=2"), change("b=2", "b=3"), READ, FINISH])
        self.james.command("step")
        self.assertEqual((self.root / "counter.py").read_text(), "a=2\nb=2\n")
        self.assertIn("Whole-file replacement rejected", str(self.model.chats))
        self.assertIn("Read the current file", str(self.model.chats))

    def test_repeated_page_triggers_single_configured_check(self):
        self.approved()
        (self.root / CONFIG).write_text(packed({"verify": [["{python}", "-c", "print('verified')"]]}))
        self.model.actions.extend([{"op": "create", "path": "counter.py", "text": "counter = 1\n"},
                                   READ, READ, FINISH])
        self.assertEqual(self.james.command("step")["event"], "completed")
        self.assertIn("verify passed", str(self.model.chats[-1]))

    def test_distillation_preserves_original(self):
        self.james.command("init")
        original = "# Lessons\n" + "- Always verify output.\n" * 31
        (self.root / LESSONS).write_bytes(original.encode())
        self.assertIn("consolidate", str(self.james.command("status")["notices"]))
        self.model.actions.append({"rules": ["Verify output."]})
        result = self.james.command("consolidate")
        self.assertEqual((self.root / result["archive"]).read_bytes(), original.encode())
        self.assertEqual((self.root / LESSONS).read_text().count("- "), 1)

    def test_distinct_symptom_starts_fresh_and_repeat_reads_are_free(self):
        self.approved()
        (self.root / "counter.py").write_text("def f():\n    return 0\n")
        a = {"op": "symptom", "path": "counter.py", "message": "Counter wraps", "line": 2}
        b = {**a, "message": "Counter stalls"}
        self.model.actions.extend([a, READ, change("return 0", "return 1"), a,
                                   READ, change("return 1", "return 2"), b, b,
                                   READ, change("return 2", "return 3"), b, FINISH])
        self.assertEqual(self.james.command("step")["event"], "paused")
        issue = self.james.ws.state()["active"]["issue"]
        self.assertEqual(len(issue["attempts"]), 1)
        self.james.command("confirm")
        self.model.actions.extend([READ, {**FINISH, "lesson": "Check stalls with a repeated input."}])
        self.assertEqual(self.james.command("step")["event"], "completed")

    def test_empty_existing_file_and_index_exports(self):
        self.approved()
        (self.root / "counter.py").touch()
        body = "def a():\n    return 1\n\ndef b():\n    return 2\n"
        self.model.actions.extend([READ, change("", body), READ, FINISH])
        self.assertEqual(self.james.command("step")["changed"]["counter.py"], ["a", "b"])
        todo = self.root / TODO
        todo.write_text(todo.read_text().replace(TITLES[1], "Update a | counter.py"))
        self.james.command("approve")
        self.model.actions.extend([READ, change("return 1\n\ndef b()", "return 0\n\ndef b()"), READ, FINISH])
        self.assertEqual(self.james.command("step")["changed"]["counter.py"], ["a"])
        self.assertIn("a, b", (self.root / INDEX).read_text(encoding="utf-8"))

    def test_recovery_and_path_validation(self):
        self.james.command("init")
        ws = Workspace(self.root)
        journal = {"new.txt": {"before": None, "text": "created"},
                   TODO: {"before": digest(ws.path(TODO).read_bytes()), "text": "# Updated\n"}}
        ws.path("new.txt").write_text("created")
        ws.path(".james/txn.json").write_text(packed(journal))
        self.james.command("init")
        self.assertEqual(ws.read(TODO), "# Updated\n")
        self.assertFalse(ws.path(".james/txn.json").exists())
        for name in ("../a", "x/../a", "C:/x", "dir\\x", "file:stream", "memory/LESSONS.md",
                     ".james/state.json", "x/.git/config", "a/NUL"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_name(name, project_file=True)

    def test_admission_under_large_multilingual_input(self):
        active = fresh_task(TITLES[0])
        active.update(observation="字" * 20000, hint="次" * 1000)
        chat = task_messages(TITLES[0], "索引" * 20000, "- " + "訓" * 4000, "界" * 5000, active, {},
                             "- " + "全" * 4000, "- tools/check.py " * 4000)
        self.assertLessEqual(size(chat[0]["content"]), SHARED)
        self.assertLessEqual(sum(size(m["content"]) for m in chat) + FRAMING + OUTPUT, WINDOW)
        with self.assertRaisesRegex(ValueError, "envelope"):
            messages("", "字" * 6000)


class Providers(unittest.TestCase):
    def test_http_contracts(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                msg = {"content": '{"tasks":[]}'}
                response = {"message": msg} if self.path == "/api/chat" else {"choices": [{"message": msg}]}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())

            def log_message(self, *_):
                pass

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                for provider in ("ollama", "openai"):
                    endpoint = base if provider == "ollama" else base + "/v1"
                    with patch.dict(os.environ, {"JAMES_PROVIDER": provider, "JAMES_ENDPOINT": endpoint}):
                        self.assertEqual(LocalModel().reply(messages("system", "input")), {"tasks": []})
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual(requests[0][1]["options"]["num_ctx"], 16384)
        self.assertEqual(requests[0][1]["options"]["num_predict"], OUTPUT)
        self.assertEqual(requests[1][0], "/v1/chat/completions")
        self.assertEqual(requests[1][1]["max_tokens"], OUTPUT)
