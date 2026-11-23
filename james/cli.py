"""Terminal adapter. Use --json for the same events consumed by future UIs."""

import argparse
import json
import sys

from .engine import James
from .model import LocalModel


def show(result, raw):
    if raw:
        print(json.dumps(result, ensure_ascii=False))
        return
    kind = result["event"]
    if kind == "completed":
        changed = [path + (" (" + ", ".join(names) + ")" if names else " (file scope)")
                   for path, names in result["changed"].items()]
        lines = [("Done", result["done"]), ("Changed", ", ".join(changed) or "No file edits"),
                 ("Spark", result["spark"])]
    elif kind == "consultation":
        print(result["packet"])
        return
    elif kind == "answer":
        print(result["text"])
        return
    elif kind == "review_required":
        print(result["todo"])
        lines = [("Review", "Edit TODO.md, then run james approve.")]
    elif kind == "state":
        print(result["todo"])
        lines = [("Phase", result["phase"])]
        if result["current"]:
            lines.append(("Current", result["current"]))
    else:
        lines = [(kind.replace("_", " ").capitalize(), " · ".join(str(v) for k, v in result.items()
                                                                  if k != "event"))]
    try:
        from rich.console import Console
        from rich.text import Text
    except ImportError:
        for label, value in lines:
            print(f"{label}  {value}")
    else:
        console = Console(highlight=False)
        for label, value in lines:
            line = Text(f"{label}  ", style="bold cyan")
            line.append(value, style="default")
            console.print(line)
    for notice in result.get("notices", []):
        print(notice, file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="james", description="Local, task-scoped coding agent")
    parser.add_argument("--workspace", "-C", default=".", help="project directory (default: current directory)")
    parser.add_argument("--json", action="store_true", help="emit a machine-readable event")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in {
        "init": "initialize project memory", "status": "show the plan and session state",
        "approve": "approve the current TODO.md", "step": "execute one approved task",
        "packet": "print the current consultation packet", "confirm": "confirm a manual behavioral fix",
        "consolidate": "distill and archive lessons",
    }.items():
        sub.add_parser(name, help=help_text)
    for name, label in {"plan": "goal", "ask": "question", "hint": "guidance", "promote": "workflow"}.items():
        p = sub.add_parser(name, help=f"{name} <{label}>")
        p.add_argument("argument", metavar=label, help="use - to read multiline text from stdin")
    args = parser.parse_args(argv)
    argument = getattr(args, "argument", "")
    if argument == "-":
        argument = sys.stdin.read()
    try:
        result = James(args.workspace, LocalModel()).command(args.command, argument)
    except (ValueError, OSError, RuntimeError) as exc:
        if args.json:
            print(json.dumps({"event": "error", "message": str(exc)}))
        else:
            print(f"James  {exc}", file=sys.stderr)
        return 1
    show(result, args.json)
    return 2 if result["event"] in {"paused", "consultation"} else 0
