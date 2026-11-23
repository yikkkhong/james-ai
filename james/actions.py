"""Project file operations and operator-defined command sequences."""

import ast
import hashlib
import json
import os
import re
import subprocess
import sys

from .workspace import CONFIG, digest


def field(obj, key, limit=400):
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"Expected a nonempty {key} (up to {limit} characters).")
    return value


def lines_at(text, start, count, numbered=False):
    lines = text.splitlines()
    offset = max(0, start - 1)
    subset = lines[offset:offset + count]
    if numbered:
        return "\n".join(f"{i}: {s}" for i, s in enumerate(subset, offset + 1))
    return "\n".join(subset)


def read_source(ws, path):
    target = ws.path(path)
    if target.stat().st_size > 2_000_000:
        raise ValueError("Target exceeds the 2 MB text-file limit.")
    data = target.read_bytes()
    if b"\x00" in data:
        raise ValueError("Task files must be UTF-8 text.")
    return data.decode("utf-8"), digest(data)


def python_symbols(source, start, end):
    try:
        tree = ast.parse(source.lstrip("\ufeff"))
    except SyntaxError:
        return None
    found = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                first = min([child.lineno] + [d.lineno for d in child.decorator_list])
                if first <= end and child.end_lineno >= start:
                    found.append(name)
                walk(child, name + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    return found


def changed_symbols(original, text, old, new, pos):
    offset = len(os.path.commonprefix([old, new]))
    pos += old[:offset].count("\n")
    removed, added = old[offset:], new[offset:]
    tail = len(os.path.commonprefix([removed[::-1], added[::-1]]))
    if tail:
        removed, added = removed[:-tail], added[:-tail]
    before = python_symbols(original, pos, pos + removed.rstrip("\r\n").count("\n")) if removed else []
    after = python_symbols(text, pos, pos + added.rstrip("\r\n").count("\n")) if added else []
    return None if before is None or after is None else before + after


def exported_symbols(source):
    try:
        tree = ast.parse(source.lstrip("\ufeff"))
    except SyntaxError:
        return None
    declarations = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    names = []
    for node in tree.body:
        if isinstance(node, declarations) and not node.name.startswith("_"):
            names.append(node.name)
            if isinstance(node, ast.ClassDef):
                names.extend(f"{node.name}.{item.name}" for item in node.body
                             if isinstance(item, declarations) and not item.name.startswith("_"))
    return names


def edit(ws, active, action, path):
    if action["op"] == "create":
        text = action.get("text")
        if not isinstance(text, str) or len(text) > 64000:
            raise ValueError("Supply UTF-8 text for the new file.")
        before, pos, old, new = None, 1, "", text
        original = ""
        if ws.path(path).exists():
            created = active.get("created", {})
            if path not in created:
                raise ValueError("This file existed before the task. Read and patch it. If it already meets the goal, return finish.")
            original, before = read_source(ws, path)
            if before != created[path]:
                raise ValueError("This file changed outside James. Read and patch the current version.")
            if original == text:
                return {}, {}
            old = original
    else:
        original, before = read_source(ws, path)
        if active["reads"].get(path) != before:
            raise ValueError("Read the current file before patching.")
        old = "" if not original and action.get("old") == "" else field(action, "old", 8192)
        new = action.get("new")
        if not isinstance(new, str) or len(new) > 16384 or old == new:
            raise ValueError("Supply a changed text replacement.")
        if "\r\n" in original and "\r\n" not in old:
            old = old.replace("\n", "\r\n")
            new = new.replace("\r\n", "\n").replace("\n", "\r\n")
        if original.count(old) != 1:
            raise ValueError("Patch anchor must match exactly once.")
        if old.strip() == original.lstrip("\ufeff").strip() and len(original.splitlines()) > 80:
            raise ValueError("Whole-file replacement rejected for a file over 80 lines. Use a smaller anchor.")
        pos = original[:original.index(old)].count("\n") + 1
        text = original.replace(old, new, 1)
    symbols = action.get("symbols", [])
    if not isinstance(symbols, list) or len(symbols) > 40 or not all(
            isinstance(s, str) and 0 < len(s) <= 100 and s.isprintable() for s in symbols):
        raise ValueError("symbols must be a short list of identifiers.")
    if path.endswith(".py"):
        actual = changed_symbols(original, text, old, new, pos)
        if actual is not None:
            symbols = actual
    touched = active["changed"].setdefault(path, [])
    touched.extend(s for s in symbols if s not in touched)
    created = active.setdefault("created", {})
    if before is None or path in created:
        created[path] = digest(text.encode("utf-8"))
    active["revision"] += 1
    active["last_edit"] = [path, pos]
    active["last_edit_op"] = action["op"]
    active["reads"].pop(path, None)
    location = f"{action['op']} {path}:{pos} ({', '.join(dict.fromkeys(symbols)) or 'file scope'})"
    old_preview = " ".join(old.split())[:180]
    new_preview = " ".join(new.split())[:180]
    detail = f"{old_preview!r} → {new_preview!r}" if old else f"added {new_preview!r}"
    active["fixes"].append(f"{location}: {detail}")
    return {path: text}, {path: before}


def workflows(ws):
    flows = json.loads(ws.read(CONFIG, "{}"))
    if not isinstance(flows, dict) or len(flows) > 32:
        raise ValueError("Expected a JSON object with up to 32 named workflows.")
    for name, steps in flows.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", name) or not isinstance(steps, list) or not 1 <= len(steps) <= 16:
            raise ValueError("Each workflow needs a short name and 1–16 commands.")
        for argv in steps:
            if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
                raise ValueError("Commands must be JSON arrays of string arguments.")
    return flows


def workflow_id(steps):
    return hashlib.sha256(json.dumps(steps).encode()).hexdigest()[:16]


def execute(root, steps):
    output = []
    for args in steps:
        argv = [sys.executable if a == "{python}" else a for a in args]
        try:
            result = subprocess.run(argv, cwd=root, shell=False, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
            diagnostic = result.stdout.decode("utf-8", "replace")
            code = result.returncode
        except subprocess.TimeoutExpired as exc:
            diagnostic = (exc.stdout or b"").decode("utf-8", "replace") + "\nCommand timed out after 90 seconds."
            code = 124
        except OSError as exc:
            diagnostic, code = str(exc), 127
        output.append(f"$ {json.dumps(args)}\n{diagnostic}")
        if code:
            return code, "\n".join(output), diagnostic
    return 0, "\n".join(output), ""


def signature(text):
    lines = [s.strip() for s in text.splitlines() if s.strip()]
    exceptions = [s for s in lines if re.match(r"(?:[\w.]+(?:Error|Exception|Failure)|AssertionError)\b", s)]
    line = (exceptions or lines or ["command failed"])[-1]
    frames = re.findall(r'File "([^"]+)", line \d+(?:, in ([^\n]+))?', text)
    frame = " ".join(frames[-1]) if frames else ""
    normalized = re.sub(r"0x[\da-fA-F]+|\b\d+(?:\.\d+)?s?\b", "?", line)
    return (frame + " " + normalized).strip()
