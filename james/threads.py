"""Per-project conversation tree. Stable IDs keep history attached across moves."""

import json
from datetime import datetime
from uuid import uuid4

from .workspace import Workspace, packed


class Threads:
    def __init__(self, project):
        directory = project.path(".james/threads")
        directory.mkdir(parents=True, exist_ok=True)
        self.store = Workspace(directory)

    def tree(self):
        return json.loads(self.store.read("tree.json", '{"main":{"kind":"folder","name":"Project","parent":null}}'))

    def history(self, key):
        if key not in self.tree():
            raise ValueError("Unknown conversation.")
        return json.loads(self.store.read(f"{key}.json", "[]"))

    def append(self, key, entry):
        with self.store.writer():
            entries = self.history(key)
            self.store.commit({f"{key}.json": packed(entries + [entry])})

    def archive_changes(self, todo, goal):
        """Prepare thread files for the project plan transaction under both writer locks."""
        if not any(line.startswith("- [") for line in todo.splitlines()):
            return {}
        tree = self.tree()
        archive = uuid4().hex
        entries = [{"type":"build", "command":"Previous plan", "result":{"event":"archived", "todo":todo}}]
        changes = {}
        for key, node in tree.items():
            if node.get("archived") or node["kind"] != "folder":
                continue
            history = self.history(key)
            builds = [e for e in history if e["type"] == "build"]
            if builds:
                entries.extend(builds)
                changes[key + ".json"] = packed([e for e in history if e["type"] != "build"])
        label = goal or next(line[6:] for line in todo.splitlines() if line.startswith("- ["))
        tree[archive] = dict(kind="folder", parent=None, archived=True,
                             name=datetime.now().strftime("%Y-%m-%d") + " · " + label[:60])
        changes.update({"tree.json":packed(tree), archive + ".json":packed(entries)})
        return {".james/threads/" + k:v for k,v in changes.items()}

    def edit(self, kind, name, parent, key=None):
        name = name.strip()
        if kind not in {"folder", "qa"} or not name or len(name) > 100 or not name.isprintable():
            raise ValueError("Choose a folder or question and a short name.")
        with self.store.writer():
            tree = self.tree()
            if key and (key not in tree or tree[key]["kind"] != kind):
                raise ValueError("Unknown conversation.")
            if parent is None and kind == "qa":
                raise ValueError("Choose a milestone for the question.")
            ancestor = parent
            while ancestor is not None:
                if ancestor == key or tree.get(ancestor, {}).get("kind") != "folder":
                    raise ValueError("Choose a parent folder outside this branch.")
                ancestor = tree[ancestor]["parent"]
            key = key or uuid4().hex
            tree[key] = dict(tree.get(key, {}), kind=kind, name=name, parent=parent)
            self.store.commit({"tree.json": packed(tree)})
            return key
