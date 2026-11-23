"""Isolated workbench step so Stop can close inference and terminate child commands."""

import json
import sys

from .engine import James
from .model import LocalModel


def send(**fields):
    print(json.dumps(fields), flush=True)


if __name__ == "__main__":
    try:
        agent = James(sys.argv[1], LocalModel(), report=lambda message: send(message=message))
        send(result=agent.command("step"))
    except (OSError, ValueError, RuntimeError) as error:
        send(result={"event":"paused", "reason":str(error)})
