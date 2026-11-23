# James 1.0

James runs one approved coding task at a time against a local chat model. The durable handoff between tasks is a Markdown artifact index. You can inspect and edit the plan in any text editor.

## Run on Windows

From this checkout, the launcher uses an installed Python 3.10+ or the Python bundled with Codex. The core has zero third-party dependencies. Start your local Ollama server, then run the following in PowerShell.

```powershell
ollama list
# If needed, download the official model once
ollama pull qwen3-coder:30b
$env:JAMES_MODEL = 'qwen3-coder:30b'
.\james.ps1 ask "How would you structure a small Python CLI?"
```

Use the exact model name your server lists. For an engine with a local alias such as `qwen3-coder-30b` or `qwen36-35b`, assign that string to `JAMES_MODEL`. To see installed models when Ollama's executable is outside PATH, run `(Invoke-RestMethod http://localhost:11434/api/tags).models.name`. On a 32 GB machine, available memory also depends on quantization, the inference backend and other running programs. James sends one request at a time per build session. [Ollama model card](https://ollama.com/library/qwen3-coder:30b)

```powershell
# Use an existing project directory, or create a new one
$project = (New-Item -ItemType Directory -Force ../greetings-demo).FullName
.\james.ps1 -C $project init
.\james.ps1 -C $project plan "Build a small Python greetings CLI with tests and a usage example."
Get-Content "$project/TODO.md"
# Edit TODO.md if desired
.\james.ps1 -C $project approve
.\james.ps1 -C $project step
.\james.ps1 -C $project status
```

Each subsequent `step` works on the next unchecked row. The terminal reports **Done**, **Changed** and **Spark**. A pause retains the current task for another invocation. Edit future rows at any time and approve the updated plan. Preserve completed rows and the in-progress row. The checked plan in this checkout records the implementation of James itself. Running `plan` in another directory creates that project's own plan.

### Install the cross-platform command

Use an environment with Python 3.10+ and pip. From this directory run the following. The optional `cli` extra adds Rich terminal rendering.

```sh
python -m pip install -e ".[cli]"
james --help
james -C /path/to/project status
```

For offline source use, `python -m james` works directly from this checkout. Global options such as `-C` and `--json` precede the command. On Windows `JAMES_PYTHON` can point the launcher at a specific interpreter.

### Local OpenAI-compatible servers

Start the engine with its model context set to **16,384 tokens**. Set the endpoint to its chat API base, including `/v1` if required. For example, adjust the following values to match your Colibri or other local server.

```powershell
$env:JAMES_PROVIDER = 'openai'
$env:JAMES_ENDPOINT = 'http://localhost:8000/v1'
$env:JAMES_MODEL = 'qwen36-35b'
.\james.ps1 ask "What is a task-scoped context window?"
```

The adapter posts to `/chat/completions` with `max_tokens=2048`. Set `JAMES_API_KEY` if the engine needs a bearer token, or `JAMES_JSON_MODE=0` if it rejects `response_format`. The response still needs to contain valid JSON for build operations. To switch back, set `JAMES_PROVIDER=ollama`, `JAMES_ENDPOINT=http://localhost:11434`, and your installed Ollama model name. Ollama receives `num_ctx=16384` and `num_predict=2048` on every call. [Ollama chat API](https://docs.ollama.com/api/chat)

## Streaming context

James shares memory across projects through `global_memory/GLOBAL_LESSONS.md` and `global_memory/GLOBAL_TOOLS.md` in the James installation. Set `JAMES_HOME` to the absolute James checkout directory when running an installed package from another location. The default is the directory containing the `james` package.

Build, plan and Q&A prompts include bounded excerpts of global lessons and tool references alongside the active project's index and lessons. Put the most important global entries first. All four sources share the existing memory budget. Q&A reads these memory files without writing them.

Capturing a lesson makes one short classification request. General language, OS, environment, tool rules and user preferences go to global lessons. Project logic and specific bugs stay in local lessons. Invalid classification pauses completion with feedback. Consolidation classifies the merged local rules, moves general rules to global memory and archives the original local file. Shared additions are locked and deduplicated, so retrying a partially completed operation safely reuses an existing global rule.

Register reusable scripts in `GLOBAL_TOOLS.md` as bullet entries with a path relative to `JAMES_HOME`, invocation and purpose, for example `- tools/check_encoding.py — python <James directory>/tools/check_encoding.py — checks UTF-8 files`. Keep the scripts in James's `tools/` directory. Register their commands in a project's `.james-workflows.json` to let James run them. Project workflow promotion continues to create project-local scripts because those sequences may depend on that project's files.

| Portion of the 16,384 window | Budget |
| --- | ---: |
| Chat-template allowance | 256 tokens |
| System rules, ranked index excerpt and up to five recent lessons | 1,382 |
| Current task, optional module, active file pages, hint and feedback | 12,698 |
| Generation for this task | 2,048 |

This reserves approximately 90% for current-task input and generation together. For Qwen's byte-level tokenizer, James conservatively charges one token per UTF-8 byte of message content. This leaves headroom and avoids a tokenizer dependency. Each request has two messages. Oversized user questions fail before transmission. James keeps the latest requested page per file and evicts older pages from the prompt as the budget fills. For custom templates or tokenizers, the serving engine's 16,384 setting provides the hard model-side ceiling.

Each plan row has the form `- [ ] Implement a named outcome | src/file.py, tests/test_file.py, memory/module.md`. Planning sees a bounded path inventory. Execution receives one row, up to one relevant memory module and requested code excerpts from that row's scope. Existing files require a fresh read and a unique, exact patch anchor. Create accepts new paths only. Text targets are UTF-8 and at most 2 MB for reading. James identifies changed Python symbols from the AST and uses model-supplied symbol annotations for other languages.

Completion commits the checkbox, index update and any local lesson, and removes the active record. Shared lessons are persisted first with duplicate checks. Intermediate diagnostics, observations and hints are evicted. The next request rebuilds its context from disk. Q&A is an independent, single-turn call with read-only memory access. Include any desired code snippet in the question.

## Commands and verification

Register project commands in `.james-workflows.json` at the project root. Each named entry is an ordered sequence of argument arrays. The repository includes a working example for James's own test suite.

```json
{
  "test": [
    ["{python}", "-m", "unittest", "discover", "-s", "tests", "-v"]
  ]
}
```

James selects a registered sequence by name. Each process runs in the project directory under your account, with a 90-second timeout, separate arguments and a closed stdin. `{python}` resolves to the current interpreter. Register trusted verification commands. Their subprocesses have ordinary OS access, including access to files outside a task's scope. The model's file tools enforce the plan's path restrictions. With configured commands James requires a passing check after the last edit. With a single registered workflow, James can run it automatically on completion or repeated read-back. Without a workflow James requires a read-back of each changed artifact. Behavioral verification then depends on your checks.

A diagnostic establishes the baseline. Read-only retries do not count as fixes. Two edited attempts followed by the same failing symptom halt the task across restarts. The consultation packet includes the complete captured command output, both patch summaries, a numbered excerpt of up to 40 source lines (the whole file when shorter), and the missing evidence to attach. Copy the packet yourself. To bring back a multiline answer from another AI:

```powershell
.\james.ps1 -C $project packet
Get-Clipboard -Raw | .\james.ps1 -C $project hint -
.\james.ps1 -C $project step
```

For a model-reported behavioral symptom, `confirm` records your own successful manual verification. A repaired task requires a reusable lesson before it can finish. Once the lesson file exceeds 30 lines, James suggests `consolidate`. That command archives the original and merges lessons with the local model. After three uses of a multi-command workflow, `promote NAME` creates `tools/NAME.py`, registers it in the index and updates the workflow to call the script.

## Project layout

```text
james.ps1                    Windows source launcher
pyproject.toml               package and james entry point
james/
  __init__.py, __main__.py    package and python -m james entry
  cli.py                     terminal rendering and JSON transport
  engine.py                  session transitions and memory maintenance
  context.py                 prompt assembly and admission limits
  model.py                   Ollama and compatible HTTP adapters
  actions.py                 text operations, symbol reporting, commands
  workspace.py               path validation, plan rows, locks, journal
  prompts/                   plan.txt, build.txt, ask.txt, consolidate.txt
tests/test_james.py           offline integration and boundary tests

Per managed project
  TODO.md                    editable plan
  memory/INDEX.md            compact artifact and symbol references
  memory/LESSONS.md          reusable rules
  memory/<module>.md         optional current-module notes
  memory/archive/            originals from consolidation
  tools/                     promoted workflows
  .james-workflows.json      optional operator-defined commands
  .james/                    local state, writer lock, temporary transaction
```

## Session API and recovery

`James(root, model).command(name, argument)` returns a JSON-serializable event. Use `--json` for the equivalent terminal envelope. For example, a completed event contains `event`, `done`, `changed` (path → symbols), `spark` and `notices`. Consumers can use `status` for `phase`, `todo`, `current` and pending notices. Build state transitions from `review` to `ready`, optionally `halted`, then `complete`. Q&A has its own entry point. A future UI can add thread IDs, image input or code selections around this interface.

CLI exit codes are 0 for a result, 1 for a rejected command or connection error, and 2 for a paused or halted task. A workspace lock serializes build writers. A pending file transaction rolls forward on the next mutating command. If a user edit conflicts with recovery, James preserves it and identifies the journal. If interrupted during an external command, inspect its effects and use `hint` to resume. The model has a 32-action limit per invocation and pauses after three consecutive invalid actions. These pauses preserve the task and are separate from a two-fix consultation.

```sh
python -m unittest discover -s tests -v
```
