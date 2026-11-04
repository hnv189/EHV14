# EHV Agent

A terminal app where a father agent delegates software work to four specialist Claude agents, one at a time. Each agent has its own skills, knowledge, pending queue, and results. A live dashboard shows all five agents in colored boxes, their current actions, streamed responses, tool calls, command output, and token usage.

```text
Your request
    -> Father inspects the project and assigns tasks
    -> Requirement agent defines acceptance criteria
    -> Design agent describes architecture and UI changes
    -> Coding agent implements in a staged project copy
    -> Testing agent validates and repairs focused defects
    -> Father reviews the handoffs and the diff
    -> Optional serial coding/testing rework
    -> Approved changes are integrated into the original project
```

There is one scheduler thread and one active model call. Tool calls also execute serially. The UI remains responsive while work runs.

## Start on Windows

Python 3.10+ is required. This checkout already has a `.venv` with the app installed. To reinstall, run `install.cmd`.

From PowerShell in this folder:

```powershell
$env:ANTHROPIC_API_KEY = "your-claude-api-key"
.\ehv-agent.cmd start --workspace "C:\path\to\your\software"
```

From Command Prompt:

```bat
set "ANTHROPIC_API_KEY=your-claude-api-key"
ehv-agent.cmd start --workspace "C:\path\to\your\software"
```

Type a request into the input at the bottom, for example:

```text
Import the calculateInvoice function from src/legacy/billing.py into the new billing service, wire it into the endpoint, and test rounding and empty inputs.
```

To open a **separate Command Prompt window**, run:

```powershell
.\ehv-agent.cmd start --new-window --workspace "C:\path\to\your\software"
```

Or use `.\launch.cmd --workspace "C:\path\to\your\software"`. The new window inherits your terminal's API key and uses the same configuration and agent folders. The dashboard uses the actual visible window size and follows resizes; it does not force console dimensions or reuse the parent terminal's `COLUMNS`/`LINES` settings. The Command Prompt stays open if the app exits with an error. Close an existing dashboard with `/quit` before opening the same agent home in a new window. The `--new-window` option is Windows-only and cannot be combined with `--headless` or `--once`.

To use the exact `ehv-agent start` command from any folder, activate this environment before changing folders:

```bat
.venv\Scripts\activate.bat
cd C:\path\to\your\software
ehv-agent start
```

PowerShell can activate with `.\.venv\Scripts\Activate.ps1`. Alternatively, install the project with `pipx install .` for a persistent global command.

## Install elsewhere

```sh
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows CMD: .venv\Scripts\activate.bat
python -m pip install -e .
export ANTHROPIC_API_KEY="your-claude-api-key"
ehv-agent start --workspace /path/to/project
```

The API key is read from `ANTHROPIC_API_KEY`; it is never saved in the app configuration. `ehv-agent doctor` checks setup without an API request. The default model is `claude-sonnet-5-5`; override it for your account with `--model MODEL` or the commands below. Each role can use a different model.

## Commands inside the dashboard

| Command | Effect |
| --- | --- |
| `/workspace C:\some\project` | Change the target project for future workflows |
| `/model MODEL` | Change the default Claude model |
| `/model MODEL coding` | Set a model just for the coding agent |
| `/models`, `/settings`, or `/model` | Scan available Claude models and open the model picker |
| `/models coding` | Open the picker for a particular agent |
| `/reload` | Validate and load edited `config.json` |
| `/pause`, `/resume` | Stop/start claiming new workflows; active work continues |
| `/cancel` | Stop active work and preserve staged files |
| `/tasks` | Show pending, running, and recent task results |
| `/retry TASK_ID` | Create a fresh attempt of a parent task |
| `/focus coding`, `/focus all` | Filter or reset the activity log |
| `/redraw` | Redraw the display using the current console viewport |
| `/help` | Show available commands |
| `/quit` | Stop work and exit |

The **Response** tab renders streamed responses and final reports with Markdown formatting, including lists, tables, bold text, and code blocks. The **Activity** tab retains the full trace of tools, command output, and formatted agent messages. Cards show a compact status and one-line preview; token counts use short labels such as `385.3k`. Click a box to focus that agent's response and log. The final father report opens the Response tab automatically.

`Ctrl+X` cancels, `Ctrl+P` pauses/resumes, `Ctrl+Q` quits, `Ctrl+L` redraws the screen, `F1` shows help, and `F2` opens model settings. A terminal of at least 100×35 is comfortable; the specialist grid uses four columns at 160 columns or wider, and two columns otherwise. Smaller terminals can scroll the agent area. The input remains above the footer. Windows console rendering enables virtual-terminal output and avoids automatic scrolling when drawing the final cell of the screen; the original output mode is restored on exit.

Workspace and model changes apply at the next workflow boundary. An active workflow retains its original configuration. Skills and knowledge are loaded when each agent starts its phase. Agent data stays in the same home folder when the target project changes.

## Scan and select Claude models

Press **F2**, or type `/models`, to open model settings. The app scans the models returned by Claude for `ANTHROPIC_API_KEY` in the app's environment. The scan runs in the background so agent progress keeps updating. Select a target and a model, then press **Save**:

- **Default model** updates the default and preserves individual role overrides.
- **All agents** sets the default and clears individual role overrides so the whole team uses the selected model.
- **Father, requirement, design, coding, or testing** sets just that agent's model.
- **Use default** for an individual agent clears its override, so it follows future default changes.

The model menu shows model names and exact API IDs. Model details include context and output limits when Claude supplies them. A selected model's advertised output limit caps the API request even if the configured `max_tokens` is higher. **Refresh** repeats the scan. Authentication, network, and empty-catalogue errors leave settings unchanged; **Cancel** closes without saving. Settings are saved in `config.json` and take effect on the next workflow.

You can also list and select models from another terminal **while the dashboard is running**, without acquiring its app lock:

```powershell
.\ehv-agent.cmd models                         # scan and list models
.\ehv-agent.cmd models --select                # choose the default from a numbered list
.\ehv-agent.cmd models --select --agent father # choose just the father's model
.\ehv-agent.cmd models --select --agent all    # apply a choice to the whole team
.\ehv-agent.cmd models --inherit --agent coding
.\ehv-agent.cmd models --json                  # metadata for scripts
```

For scripts, `models --set MODEL_ID --agent ROLE` scans and validates the requested ID before saving. Add `--home PATH` to configure a particular agent home. CLI selections affect future workflows; `/reload` refreshes the open dashboard's labels. Model scanning calls Claude's [List Models API](https://platform.claude.com/docs/en/api/python/models/list); it requests metadata and does not generate a response with a model.

## Agent folders and durable scheduling

The default agent home is `.ehv-agent` in the folder where you start the app. Subsequent commands discover the nearest `.ehv-agent` in the current directory or its ancestors. Use `--home` on any command to select an explicit, reusable home:

```sh
ehv-agent start --home C:/agents/my-team --workspace C:/projects/my-app
ehv-agent enqueue --home C:/agents/my-team "Add CSV export with tests"
```

```text
.ehv-agent/
  config.json
  events.jsonl                  # full event stream; includes visible model output
  agents/
    father/
      skills/ROLE.md
      knowledge/
      pending/                  # new workflow requests
      running/
      results/                  # JSON metadata and Markdown reports
    requirement/                # same five folders for every role
    design/
    coding/
    testing/
  runs/<task-id>/
    request.json
    baseline.json
    plan.json
    handoffs.json
    review.json
    project/                    # staged project copy
    backups/                    # original files replaced or deleted on integration
    integration.json            # integration journal and changed-file manifest
    outcome.json
```

Put role instructions and procedures in each `skills/` folder, and reference information in `knowledge/`. Their paths are included in the role's prompt and the agent can read them on demand. The default `skills/ROLE.md` is always included. Agents can save Markdown knowledge for future workflows. You can also edit role instructions and model IDs in `config.json` under `agents`.

The scheduler scans pending folders every second by default. To add a task from another process, use `enqueue` or atomically rename a complete JSON file into `agents/father/pending/your-task.json`:

```json
{
  "prompt": "Add CSV export to the reports page and cover empty reports",
  "not_before": "2026-11-05T09:00:00+07:00"
}
```

`not_before` is optional and must include a timezone. This is a one-shot queue, not a recurring cron scheduler. The app must be running for jobs to execute. CLI equivalent:

```sh
ehv-agent enqueue "Add CSV export" --at "2026-11-05T09:00:00+07:00"
```

You can submit a direct specialist task with `ehv-agent enqueue --agent coding "Fix the parser"`, or place the JSON in that agent's pending folder. The father still reviews it, and coding changes receive a testing pass before integration. Child tasks generated by the father have a `parent_id` and are consumed in the father's serial order.

Tasks run oldest first across all roles. Invalid task JSON is moved to results with an error. On restart, interrupted tasks are marked `interrupted`; they are never silently replayed. Use `/retry` with the parent ID to start a fresh workflow. Staged work and reports remain available for inspection. One app instance can own an agent home at a time.

## Project tools

- List files, read line ranges, and search literal text.
- Write files, replace unique text, and delete staged files (coding/testing roles).
- Run build, test, and development commands with streamed output, timeout, cancellation, and captured exit codes.
- Read role-specific skills/knowledge and save reusable Markdown knowledge.
- Inspect a changed-file manifest and unified diffs.
- Submit a validated plan, structured specialist handoff, and father review decision.

Command tools accept argument arrays and run without a shell. Available executables are Python, pytest, Node, npm/npx, pnpm, yarn, uv, Ruff, mypy, Cargo, Go, dotnet, make, and CMake when installed. On Windows, common npm shims are resolved to their JavaScript entrypoints and invoked through Node. Commands run with the app's interpreter/PATH in the staged project; agents may need to install dependencies there.

Commands execute project code with your user permissions. Staging and path checks protect ordinary file tools; they are not an operating-system sandbox. The app removes the Claude and OpenAI API key variables from subprocess environments, but this is not credential isolation.

## Integration and failure behavior

Project files are copied into `runs/<id>/project` before the first model call. By default the snapshot excludes `.git`, agent state, dependency directories, virtual environments, build output, `.env` files, and common private-key extensions. Symbolic links are skipped in the source; new staged symbolic links block integration. Edit `excludes` in the configuration for your project. File content changes are integrated; empty directories and permission-only changes are not tracked.

The father reviews reports and changes after testing, and can request a bounded serial coding/testing repair pass (`max_rework`, default 1). A failed or blocked latest specialist result prevents integration, even if the model approves. The app cannot independently prove a model's semantic assessment, so reports retain command evidence and explicit limitations.

Approved changes are copied back to their relative paths in the original project. Before touching files, integration checks their hashes against the original snapshot. If you edited an affected file while agents worked, integration stops with a conflict and preserves the stage. Unaffected user edits remain in place. Backups and an integration journal are saved before writes; normal exceptions or cancellation during application trigger rollback of touched files. A process kill or power failure can interrupt the multi-file transaction: inspect `integration.json` and `backups/` before retrying. File writes are atomic individually, not as a whole project.

## Headless use and configuration

```sh
ehv-agent init --workspace /path/to/project
ehv-agent config workspace /another/project
ehv-agent config poll_seconds 2
ehv-agent config max_turns 60
ehv-agent config max_tokens 12000
ehv-agent config command_timeout 180
ehv-agent config max_rework 2
ehv-agent tasks
ehv-agent start --headless     # continuously watch queues, emit JSONL events
ehv-agent start --once         # process one due workflow, return nonzero on failure
```

`config` with no key shows the complete configuration. Headless operation uses the same scheduler, queue folders, and tools as the dashboard. No automatic spend cap is implemented; model turns, output tokens, command duration, and rework passes have configurable bounds. Events, stages, and backups are retained until you remove them.

## Development

```sh
python -m pip install -e '.[dev]'
python -m pytest -q
ruff check src tests
```

Tests cover the full serial workflow, rework, failed-test integration blocking, workspace switching, queues, restart recovery, real SDK streaming through an in-memory HTTP transport, tool permissions, commands, cancellation, path boundaries, conflict detection, rollback, and a headless Textual dashboard. On Windows, a hidden native-console check also runs the real rendering driver, injects a stale buffer-size notification, and checks that the prompt and footer are drawn once. No API key or paid API calls are needed for tests. A live Claude request must be verified separately with your key.

The API integration follows Anthropic's [streaming protocol](https://platform.claude.com/docs/en/build-with-claude/streaming) and [Python SDK](https://github.com/anthropics/anthropic-sdk-python). Model IDs are configurable; see the [Claude model list](https://platform.claude.com/docs/en/models/overview).
