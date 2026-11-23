import importlib.util
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec("fastapi") and importlib.util.find_spec("uvicorn"), "requires ui extra")
class Workbench(unittest.TestCase):
    def test_stop_closes_pending_step_and_releases_workspace(self):
        from james.live import LiveStep
        from james.engine import James
        from james.workspace import render_tasks

        waiting, release = threading.Event(), threading.Event()

        class SlowModel(BaseHTTPRequestHandler):
            def do_POST(self):
                waiting.set()
                release.wait(10)

        with tempfile.TemporaryDirectory() as d, HTTPServer(("127.0.0.1",0), SlowModel) as server:
            threading.Thread(target=server.handle_request, daemon=True).start()
            agent = James(d, None)
            agent.command("init")
            todo = render_tasks([(False, f"Build item {i} | file{i}.py") for i in range(5)])
            (Path(d) / "TODO.md").write_text(todo)
            agent.command("approve")
            live = LiveStep()
            results = []
            endpoint = f"http://127.0.0.1:{server.server_port}"
            with patch.dict(os.environ, {"JAMES_PROVIDER":"ollama", "JAMES_ENDPOINT":endpoint}):
                worker = threading.Thread(target=lambda: results.append(live.run(agent.ws, "main")))
                worker.start()
                try:
                    self.assertTrue(waiting.wait(6))
                    snapshot = live.snapshot()
                    self.assertIn("Action 1/8", snapshot["events"][0])
                    live.stop(snapshot["id"])
                    worker.join(3)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(results[0]["event"], "paused")
                    self.assertIn("Stopped", results[0]["reason"])
                    with agent.ws.writer():
                        self.assertEqual(agent.ws.state()["completed"], [])
                finally:
                    release.set()
                    worker.join(5)

    def test_workspace_flow(self):
        import uvicorn
        from james.workbench import app_for

        class Model:
            def reply(self, chat, structured=True, image=None):
                self.prompt = chat[-1]["content"]
                if not structured:
                    return "An explanation."
                return {"tasks": [f"Implement feature {n} | feature{n}.py" for n in range(5)]}

        model = Model()
        with tempfile.TemporaryDirectory() as d, socket.socket() as sock:
            root = Path(d)
            src = root / "sample.py"
            src.write_text("before\nselected\nafter\n")
            app = app_for(root, model)
            sock.bind(("127.0.0.1", 0))
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            worker = threading.Thread(target=server.run, kwargs={"sockets":[sock]}, daemon=True)
            worker.start()
            try:
                for _ in range(100):
                    if server.started:
                        break
                    time.sleep(.02)
                url = f"http://127.0.0.1:{sock.getsockname()[1]}/"
                def call(path, data=None):
                    request = urllib.request.Request(url + path, data=json.dumps(data).encode() if data else None,
                                                     headers={"Content-Type":"application/json", "X-James-UI":"1"})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return json.load(response)
                with urllib.request.urlopen(url) as response:
                    self.assertIn(b"Prototype from Sketch", response.read())
                folder = call("api/node", dict(kind="folder", name="Authentication"))["key"]
                qa = call("api/node", dict(kind="qa", name="Why auth?", parent=folder))["key"]
                stamp = call("api/file?path=sample.py")["fingerprint"]
                call("api/ask", dict(thread=qa, path="sample.py", start=2, end=2, fingerprint=stamp, text="Why?"))
                self.assertIn("before\nSelected lines\nselected\nFollowing lines\nafter", model.prompt)
                self.assertFalse((root/"TODO.md").exists())
                self.assertEqual(src.read_text(), "before\nselected\nafter\n")
                call("api/node", dict(kind="qa", key=qa, name="Implementation notes", parent="main"))
                self.assertEqual(len(call("api/history?thread="+qa)), 1)
                result = call("api/build", dict(thread=folder, op="sketch", text="[Sidebar] | [Code]"))
                self.assertEqual(result["event"], "review_required")
                self.assertIn("[Sidebar]", model.prompt)
                call("api/build", dict(thread=folder, op="approve"))
                call("api/build", dict(thread=qa, op="promote", text="Clarify behavior | sample.py"))
                self.assertEqual(call("api/state")["todo"].count("[ ]"), 6)
                self.assertEqual(call("api/state")["phase"], "review")
                self.assertEqual(len(call("api/history?thread="+folder)), 2)
                prior = call("api/state")["todo"]
                call("api/build", dict(thread=folder, op="plan", text="Start another milestone"))
                trees = call("api/state")["tree"]
                archived = next(key for key in trees if trees[key].get("archived"))
                self.assertEqual(call("api/history?thread="+archived)[0]["result"]["todo"], prior)
                self.assertEqual(len(call("api/history?thread="+folder)), 1)
                self.assertEqual(len(call("api/history?thread="+qa)), 1)
            finally:
                server.should_exit = True
                worker.join(5)
