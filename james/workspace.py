"""Workspace paths, a single writer lock and recoverable multi-file commits."""

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath

TODO = "TODO.md"
INDEX = "memory/INDEX.md"
LESSONS = "memory/LESSONS.md"
CONFIG = ".james-workflows.json"
STATE = ".james/state.json"
SEEDS = {TODO: "# Tasks\n", INDEX: "# Artifact index\n", LESSONS: "# Lessons\n"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def packed(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def safe_name(name, project_file=False):
    if isinstance(name, str):
        name = re.sub(r"^(?:/path/to/workspace/|\.[/\\])+", "", name)
    if not isinstance(name, str) or not name or len(name) > 240:
        raise ValueError("Expected a relative file path (1–240 characters).")
    parts = name.split("/")
    for part in parts:
        if (part in {"", ".", ".."} or part.endswith((" ", ".")) or
                re.search(r'[<>:"\\|?*\x00-\x1f]', part) or PureWindowsPath(part).is_reserved()):
            raise ValueError(f"Invalid workspace path: {name!r}")
    key = name.casefold()
    protected = {".git", ".james", ".codex", ".agents", ".aws", ".ssh", ".env", "global_memory"}
    if project_file and (any(p.casefold() in protected for p in parts) or
                         key in {TODO.casefold(), INDEX.casefold(), LESSONS.casefold(), CONFIG} or
                         key.startswith("memory/archive/")):
        raise ValueError(f"Reserved project path: {name}")
    return name


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        tmp = Path(stream.name)
    try:
        tmp.write_bytes(data)
        if path.exists():
            tmp.chmod(path.stat().st_mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class Workspace:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Workspace must be an existing directory.")

    def path(self, name):
        path = self.root / safe_name(name)
        if path.resolve() != path or not path.resolve().is_relative_to(self.root):
            raise ValueError(f"Linked paths cannot participate in a task: {name}")
        return path

    def read(self, name, default=None):
        path = self.path(name)
        if default is not None and not path.exists():
            return default
        return path.read_bytes().decode("utf-8-sig")

    def snapshot(self, name):
        data = self.path(name).read_bytes()
        return data.decode("utf-8-sig"), digest(data)

    def state(self):
        return json.loads(self.read(STATE, '{"phase":"idle","completed":[],"approved":[],"workflows":{}}'))

    @contextmanager
    def writer(self):
        path = self.path(".james/lock")
        path.parent.mkdir(exist_ok=True)
        with path.open("a+b") as lock:
            try:
                if os.name == "nt":
                    import msvcrt
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Another James command is using this workspace.") from exc
            self.recover()
            yield

    def commit(self, changes, expected=None):
        journal = {}
        for name, content in changes.items():
            path = self.path(name)
            before = digest(path.read_bytes()) if path.exists() else None
            if expected and name in expected and expected[name] != before:
                raise ValueError(f"{name} changed on disk. Read it again.")
            journal[name] = {"before": before, "text": content}
        atomic(self.path(".james/txn.json"), packed(journal).encode())
        self.recover()

    def recover(self):
        txn = self.path(".james/txn.json")
        if not txn.exists():
            return
        pending = []
        for name, item in json.loads(txn.read_bytes()).items():
            path = self.path(name)
            before = digest(path.read_bytes()) if path.exists() else None
            after = item["text"].encode()
            if before == digest(after):
                continue
            if before != item["before"]:
                raise RuntimeError(f"Recovery conflict in {name}. Preserve your edits and inspect {txn}.")
            pending.append((path, after))
        for path, data in pending:
            atomic(path, data)
        txn.unlink()

    def init(self):
        missing = {k: v for k, v in SEEDS.items() if not self.path(k).exists()}
        if missing:
            self.commit(missing)
        for folder in ("tools", "memory/archive"):
            self.path(folder).mkdir(parents=True, exist_ok=True)

    def inventory(self):
        result = []
        for base, dirs, files in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in
                             {"node_modules", "__pycache__", "dist", "build", "archive", "venv"}
                             and (Path(base) / d).resolve() == Path(base) / d)
            for name in sorted(files):
                rel = (Path(base) / name).relative_to(self.root).as_posix()
                try:
                    safe_name(rel, project_file=True)
                except ValueError:
                    continue
                result.append(rel)
                if len(result) == 200:
                    return result
        return result


def parse_title(title):
    if not isinstance(title, str) or len(title.encode()) > 720 or not title.isprintable():
        raise ValueError("Each task needs one short title and its target paths.")
    label, sep, targets = title.partition(" | ")
    if not sep or not label.strip() or len(label) > 220:
        raise ValueError("Use one row per task: - [ ] Title | file.py, tests/test_file.py")
    paths = [safe_name(p.strip(), project_file=True) for p in targets.split(",")]
    if len(paths) > 8 or len(set(paths)) != len(paths):
        raise ValueError("Use 1–8 distinct target files per micro-task.")
    if sum(p.casefold().startswith("memory/") for p in paths) > 1:
        raise ValueError("Select at most one memory module per task.")
    return paths


def rows(markdown):
    tasks = []
    for line in markdown.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = re.fullmatch(r"- \[([ x])\] (.+)", line)
        if not match:
            raise ValueError(f"Invalid TODO row: {line[:100]}")
        parse_title(match[2])
        tasks.append((match[1] == "x", match[2]))
    if not 5 <= len(tasks) <= 15 or len({t for _, t in tasks}) != len(tasks):
        raise ValueError("A plan needs 5–15 distinct tasks.")
    return tasks


def render_tasks(tasks):
    return "# Tasks\n\n" + "".join(f"- [{'x' if done else ' '}] {title}\n" for done, title in tasks)
