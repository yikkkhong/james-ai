"""Shared lessons and tool references, independent of the managed project."""

import os
from pathlib import Path

from .context import messages, prompt
from .workspace import Workspace


class GlobalMemory:
    def __init__(self):
        home = Path(os.getenv("JAMES_HOME", Path(__file__).resolve().parent.parent))
        self.root = home / "global_memory"

    def read(self, name):
        path = self.root / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def add(self, rules):
        if not rules:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        ws = Workspace(self.root)
        with ws.writer():
            original = ws.read("GLOBAL_LESSONS.md", "# Global lessons\n")
            known = {s.removeprefix("- ").strip().casefold() for s in original.splitlines()}
            additions = []
            for rule in rules:
                if rule.casefold() not in known:
                    additions.append(f"- {rule}\n")
                    known.add(rule.casefold())
            if additions:
                ws.commit({"GLOBAL_LESSONS.md": original.rstrip() + "\n" + "".join(additions)})


def classify(model, rules, task):
    import json

    answer = model.reply(messages(prompt("route"), json.dumps({"task": task, "rules": rules})))
    scopes = answer.get("scopes")
    if not isinstance(scopes, list) or len(scopes) != len(rules) or any(
            scope not in {"project", "global"} for scope in scopes):
        raise ValueError("Lesson classification requires one project/global scope per rule.")
    return ([r for r, s in zip(rules, scopes) if s == "project"],
            [r for r, s in zip(rules, scopes) if s == "global"])
