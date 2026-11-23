"""Stateless message construction with a conservative byte-token envelope."""

import json
from pathlib import Path

WINDOW = 16384
OUTPUT = 2048
FRAMING = 256
SHARED = WINDOW // 10 - FRAMING
FOCUS = WINDOW - OUTPUT - FRAMING - SHARED
PROMPTS = Path(__file__).with_name("prompts")


def size(text):
    return len(text.encode("utf-8"))


def clip(text, limit):
    if size(text) <= limit:
        return text
    marker = "\n[…]"
    if limit < size(marker):
        return ""
    return text.encode()[:limit - size(marker)].decode("utf-8", "ignore") + marker


def prompt(name):
    return (PROMPTS / f"{name}.txt").read_text(encoding="utf-8").strip()


def messages(system, user):
    if size(system) + size(user) + FRAMING + OUTPUT > WINDOW:
        raise ValueError("Input exceeds the 16,384-token envelope. Shorten or page the input.")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def memory_context(index, lessons, global_lessons, global_tools, budget):
    sections = {"INDEX": index,
                "LESSONS": "\n".join(reversed([s for s in lessons.splitlines() if s.startswith("- ")][-5:])),
                "GLOBAL LESSONS": "\n".join(s for s in global_lessons.splitlines() if s.startswith("- ")),
                "GLOBAL TOOLS": "\n".join(s for s in global_tools.splitlines() if s.startswith("- "))}
    labels = sum(size("\n" + label + "\n") for label in sections)
    allowance = max(0, (budget - labels) // len(sections))
    return "".join("\n" + label + "\n" + clip(content, allowance) for label, content in sections.items())


def task_messages(title, index, lessons, module, active, flows, global_lessons="", global_tools=""):
    rules = prompt("build")
    # Prioritize index rows referencing the active scope and the newest lessons.
    paths = title.split(" | ")[1].split(", ")
    rows = sorted(index.splitlines(), key=lambda row: not any(p in row for p in paths))
    rules += memory_context("\n".join(rows), lessons, global_lessons, global_tools, SHARED - size(rules))
    facts = {"current_task": title, "workflows": list(flows), "already_changed": list(active["changed"]),
             "lesson_required": bool(active.get("had_bug") or active.get("hint"))}
    header = json.dumps(facts, ensure_ascii=False)
    if size(header) > FOCUS // 2:
        raise ValueError("Task metadata too large. Reduce workflow names or the task scope.")
    focus = header + "\nModule\n" + clip(module, 1600)
    focus += "\nUser hint\n" + active.get("hint", "")
    issue = active.get("issue")
    if issue:
        focus += "\nOpen failure\n" + clip(issue["diagnostic"][-1600:], 1600)
    focus += "\nCurrent observation\n"
    guide = "\nChoose the next single action for this task."
    if active["changed"] and not issue:
        pending = [path for path in active["changed"] if path not in active["reads"]]
        if flows and active["verified"] != active["revision"]:
            guide = "\nWhen implementation is ready, run a listed verification workflow."
        elif not flows and pending:
            guide = "\nRead back your changes to " + ", ".join(pending) + " before finishing."
        else:
            guide = "\nChanges have been checked. If the current task is satisfied, return the finish JSON now."
    budget = FOCUS - size(focus) - size(guide)
    if budget < 300:
        raise ValueError("Shorten the hint to leave room for current observations.")
    observation = "\n".join([*active.get("pages", {}).values(), active.get("observation", "")])
    if size(observation) > budget:
        observation = observation.encode()[-budget:].decode("utf-8", "ignore")
    return messages(rules, focus + observation + guide)
