"""Loopback FastAPI adapter for the James session engine."""

import base64
import asyncio
import json
import socket
import struct
import threading
import webbrowser
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .actions import read_source
from .context import OUTPUT, WINDOW, clip, messages
from .engine import James
from .model import LocalModel
from .live import LiveStep
from .threads import Threads
from .workspace import TODO, parse_title, rows, safe_name


class Node(BaseModel):
    kind: Literal["folder", "qa"]
    name: str
    parent: str | None = None
    key: str | None = None


class Question(BaseModel):
    thread: str
    text: str = Field(min_length=1, max_length=3000)
    path: str | None = None
    start: int = Field(default=1, ge=1)
    end: int = Field(default=1, ge=1)
    fingerprint: str = ""


class Build(BaseModel):
    thread: str
    op: Literal["plan", "approve", "step", "hint", "confirm", "sketch", "promote"]
    text: str = Field(default="", max_length=6000)
    image: str = Field(default="", max_length=1600000)


def app_for(root, model=None):
    agent = James(root, model or LocalModel())
    ws = agent.ws
    threads = Threads(ws)
    live = LiveStep()
    app = FastAPI(docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method == "POST" and request.headers.get("x-james-ui") != "1":
            return JSONResponse({"detail": "Use the local workbench."}, status_code=403)
        return await call_next(request)

    def failed(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    for error in (ValueError, OSError, RuntimeError):
        app.add_exception_handler(error, failed)

    @app.get("/api/live")
    async def stream(request: Request):
        async def updates():
            previous = None
            while not await request.is_disconnected():
                value = live.snapshot()
                if value != previous:
                    yield "data: " + json.dumps(value) + "\n\n"
                    previous = value
                await asyncio.sleep(.15)
        return StreamingResponse(updates(), media_type="text/event-stream", headers={"Cache-Control":"no-store"})

    @app.post("/api/stop/{run_id}")
    def stop(run_id: str):
        live.stop(run_id)
        return {"event":"stop_requested"}

    @app.get("/api/state")
    def state():
        session = ws.state()
        active = session.get("active") or {}
        return dict(**agent.status(), project=ws.root.name, tree=threads.tree(),
                    behavior=(active.get("issue") or {}).get("source") == "behavior",
                    model=getattr(agent.model, "name", "Local model"), budget=active.get("budget"),
                    window=WINDOW, output=OUTPUT)

    @app.get("/api/files")
    def files(directory: str = ""):
        folder = ws.path(safe_name(directory, True)) if directory else ws.root
        entries = []
        for item in folder.iterdir():
            if item.name.startswith(".") or item.name in {"__pycache__", "node_modules"}:
                continue
            path = item.relative_to(ws.root).as_posix()
            try:
                ws.path(safe_name(path, True))
            except ValueError:
                continue
            entries.append(dict(path=path, name=item.name, folder=item.is_dir()))
        return sorted(entries, key=lambda f: (not f["folder"], f["name"].casefold()))

    @app.get("/api/file")
    def file(path: str):
        source, stamp = read_source(ws, safe_name(path, True))
        return dict(text=source.replace("\r\n", "\n"), fingerprint=stamp)

    @app.post("/api/node")
    def node(body: Node):
        return {"key": threads.edit(body.kind, body.name, body.parent, body.key)}

    @app.get("/api/history")
    def history(thread: str):
        return threads.history(thread)

    @app.post("/api/ask")
    def ask(q: Question):
        previous = threads.history(q.thread)
        recent = [e for e in previous if e["type"] == "qa"][-3:]
        content = "Recent questions and answers\n" + clip(json.dumps(recent, ensure_ascii=False), 2200)
        if q.path:
            source = file(q.path)
            lines = source["text"].split("\n")
            if source["fingerprint"] != q.fingerprint or not q.start <= q.end <= len(lines):
                raise ValueError("Reopen the file and select the current lines.")
            selected = "\n".join(lines[q.start-1:q.end])
            if len(selected.encode()) > 5500:
                raise ValueError("Select a shorter excerpt.")
            content += f"\n{q.path} lines {q.start}–{q.end}\n"
            content += clip("\n".join(lines[max(0,q.start-5):q.start-1]), 600)
            content += "\nSelected lines\n" + selected + "\nFollowing lines\n"
            content += clip("\n".join(lines[q.end:q.end+4]), 600)
        answer = agent.ask(content + "\nQuestion\n" + q.text)["text"]
        entry = dict(type="qa", question=q.text, answer=answer, path=q.path, start=q.start, end=q.end)
        threads.append(q.thread, entry)
        return entry

    @app.post("/api/build")
    def build(b: Build):
        tree = threads.tree()
        if b.thread not in tree:
            raise ValueError("Select a milestone.")
        milestone = b.thread if tree[b.thread]["kind"] == "folder" else tree[b.thread]["parent"]
        op, argument = b.op, b.text
        if tree[milestone].get("archived"):
            raise ValueError("Select a current milestone to build.")
        before = ws.state()
        current = next((t for t in before["approved"] if t not in before["completed"]), None)
        if op == "step":
            result = live.run(ws, milestone)
        elif op == "plan":
            with ws.writer(), threads.store.writer():
                ws.init()
                session = ws.state()
                archived = threads.archive_changes(ws.read(TODO), session.get("goal"))
                result = agent.plan(session, argument, archive=archived)
            live.reset()
        elif op == "promote" and before["phase"] not in {"idle", "complete"}:
            parse_title(argument)
            with ws.writer():
                old, stamp = ws.snapshot(TODO)
                new = old.rstrip() + "\n- [ ] " + argument + "\n"
                rows(new)
                session = ws.state()
                if session["phase"] != "halted":
                    session["phase"] = "review"
                agent.save(session, {TODO: new}, {TODO: stamp})
            result = {"event":"review_required", "todo":new}
        else:
            if op == "sketch":
                if before["phase"] not in {"idle","review","complete"}:
                    raise ValueError("Finish the approved plan before planning a prototype.")
                if b.image:
                    raw = base64.b64decode(b.image, validate=True)
                    if (len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n" or
                            max(struct.unpack(">II", raw[16:24])) > 512):
                        raise ValueError("Use a PNG thumbnail up to 512 pixels.")
                    description = agent.model.reply(messages("Describe the wireframe layout and visual hierarchy.", "Read this sketch."),
                                                    structured=False, image=b.image)
                    argument += "\n" + clip(description, 3200)
                if not argument.strip():
                    raise ValueError("Provide a sketch or layout description.")
                argument = "Plan a minimalist UI prototype using my global design preferences. Layout:\n" + argument
            result = agent.command("plan" if op in {"sketch","promote"} else op, argument)
        threads.append(milestone, dict(type="build", command=op, task=current if op=="step" else None, result=result))
        if op != "step":
            live.reset()
        return result

    @app.get("/api/packet")
    def packet():
        return agent.packet(ws.state(), "")

    app.mount("/", StaticFiles(directory=Path(__file__).with_name("web"), html=True), name="ui")
    return app


def serve(root, port, open_browser):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))
        actual = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app_for(root), access_log=False))
        if open_browser:
            threading.Timer(1, webbrowser.open, args=(f"http://127.0.0.1:{actual}/",)).start()
        print(f"James workbench  http://127.0.0.1:{actual}/", flush=True)
        server.run(sockets=[sock])
