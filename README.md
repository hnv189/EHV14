# EHV Agent

A terminal app where a father agent classifies each request, answers simple questions itself, and delegates work to the specialists it needs, one at a time. Four specialist Claude agents cover requirements, design, coding, and testing. Each agent has its own skills, knowledge, pending queue, and results. A live dashboard shows all five agents in colored boxes, their current actions, streamed responses, tool calls, command output, and token usage.

```text
Your request
    -> Father classifies the task and inspects relevant evidence
       -> Simple question or explanation: father answers directly
       -> Specialist work: father selects only the necessary roles
          -> Requirement / design / coding / testing, in serial order
          -> Any project changes receive independent testing
          -> Father reviews the handoffs and the diff
          -> Optional serial coding/testing rework
          -> Approved changes are integrated into the original project
```

The father classifies requests as **question**, **analysis**, **implementation**, or **validation**, and records the type and routing reason in `plan.json`. Simple questions and small read-only investigations finish with the father's answer, without child tasks or a separate review call. More involved analysis can use just requirement and design; a clear code fix can go straight to coding and testing; a validation request can use testing alone. All four specialists are used only when needed. The choice comes from the father's assessment of the request and project context, not keyword matching.

There is one scheduler thread and one active model call. Tool calls also execute serially. The UI remains responsive while work runs.

## Start on Windows

The app runs from a conda environment in `.conda-env`, which requires conda (Miniconda or Anaconda). Run `install.cmd` to create the environment and install the app, or to reinstall it.

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
conda activate .\.conda-env
cd C:\path\to\your\software
ehv-agent start
```

Run this from an Anaconda Prompt, or from a shell where conda is initialized (`conda init powershell` for PowerShell). Alternatively, install the project with `pipx install .` for a persistent global command.

## Install elsewhere

```sh
conda create -y -p .conda-env python=3.12 -c conda-forge --override-channels
conda activate ./.conda-env
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
| `/models`, `/settings`, or `/model` | Open model and proxy settings and scan available Claude models |
| `/models coding` | Open the picker for a particular agent |
| `/reload` | Validate and load edited `config.json` |
| `/pause`, `/resume` | Stop/start claiming new workflows; active work continues |
| `/cancel` | Stop active work and preserve staged files |
| `/tasks` | Show pending, running, and recent task results |
| `/sessions` | Open the Recent Sessions tab |
| `/sessions SESSION_ID` | Open a saved conversation; choose Continue session to add another ask |
| `/newsession` | Start a fresh conversation, keeping earlier sessions saved |
| `/retry TASK_ID` | Create a fresh attempt of a parent task |
| `/focus coding`, `/focus all` | Filter or reset the activity log |
| `/redraw` | Redraw the display using the current console viewport |
| `/help` | Show available commands |
| `/quit` | Stop work and exit |

The **Response** tab is a scrollable conversation: every ask and its father answer stay in order, with Markdown formatting for lists, tables, code blocks, and other content. New asks append below earlier exchanges. Scroll up to read an older answer; incoming text keeps your scroll position, and the view follows new text when you are already at the bottom. The **Activity** tab retains the full trace of tools, command output, and formatted agent messages. Cards show compact status and token counts such as `385.3k`. Click a card to inspect that agent's latest response and log; `/focus all` returns to the full conversation.

Each ask–answer exchange shows its total estimated USD cost, including the father, children, tools' API turns, compaction, retries, and review. The Response and workspace headers show the sum across **all exchanges in the selected session**. Agent cards show each role's subtotal for that session. Starting the next ask keeps those totals; `/newsession` starts a new total. Raw API-response costs remain available in Activity and each workflow's saved usage ledger. Recent Sessions includes **Est. USD** and **Exchanges** columns.

The current session and its earlier asks/answers survive app restarts. Follow-up requests receive recent completed exchanges as context; older details are available through `read_session_history`. Future queued asks are excluded from model context. `/newsession` starts a separate conversation and does not cancel active work: that work and its costs stay with its original session.

`Ctrl+X` cancels, `Ctrl+P` pauses/resumes, `Ctrl+Q` quits, `Ctrl+L` redraws the screen, `F1` shows help, `F2` opens model settings, and `F3` opens Recent Sessions. A terminal of at least 100×35 is comfortable; the specialist grid uses four columns at 160 columns or wider, and two columns otherwise. Smaller terminals can scroll the agent area. The input remains above the footer. Windows console rendering enables virtual-terminal output and avoids automatic scrolling when drawing the final cell of the screen; the original output mode is restored on exit.

Model and connection changes apply at the next workflow boundary. A conversation keeps its workspace, including for queued asks. Changing `/workspace` selects a fresh conversation in the new project; queued work retains its original project. Skills and knowledge are loaded when each agent starts its phase. Agent data stays in the same home folder when the target project changes.

## Scan and select Claude models

Press **F2**, or type `/settings` or `/models`, to open model and connection settings. The app scans the models returned by Claude for `ANTHROPIC_API_KEY` in the app's environment. The scan runs in the background so agent progress keeps updating. Select a target and a model, then press **Save**:

- **Default model** updates the default and preserves individual role overrides.
- **All agents** sets the default and clears individual role overrides so the whole team uses the selected model.
- **Father, requirement, design, coding, or testing** sets just that agent's model.
- **Use default** for an individual agent clears its override, so it follows future default changes.

The model menu shows model names and exact API IDs. Model details include context and output limits when Claude supplies them. A selected model's advertised output limit caps the API request even if the configured `max_tokens` is higher. **Refresh** repeats the scan. Authentication, network, and empty-catalogue errors leave model settings unchanged; **Cancel** discards unsaved edits. Settings are saved in `config.json` and take effect on the next workflow.

The **Connection** section offers **Environment defaults** (the default; uses `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, and `NO_PROXY`), **Direct connection** (ignores environment proxies), and **Custom HTTP/HTTPS proxy**. For a custom proxy, enter a full URL such as `http://proxy.example.com:8080`, then press **Save proxy**. Saving works even when model scanning fails and automatically retries the scan with the saved connection. The same settings apply to all agents and CLI model scans. Active workflows keep their original connection settings. These controls configure Claude connections; project commands keep their own environment settings.

Proxy URLs can include URL-encoded credentials (`http://user:password@proxy.example.com:8080`). The URL field is masked; the complete URL is saved locally in `config.json`. **Save proxy** saves independently of the model selection, so closing the dialog keeps proxy settings already saved. Existing configurations without proxy fields continue using environment defaults.

You can also configure a proxy from the terminal:

```powershell
.\ehv-agent.cmd config proxy_url "http://proxy.example.com:8080"
.\ehv-agent.cmd config proxy_mode custom
# Restore environment defaults, or use direct to bypass environment proxies:
.\ehv-agent.cmd config proxy_mode system
```

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

## Recent sessions

Click **Recent Sessions**, press **F3**, or type `/sessions` to see the latest 100 conversations across the workspaces used by this agent home. Each row shows the title, status, total cost, exchange count, local creation time, and workspace. Asks sharing a conversation appear in one row; child assignments do not appear as separate sessions. The list refreshes as work progresses and survives app restarts.

The conversation takes its title from its first named exchange. The father names exchanges during planning and can refine their titles at final review. Titles describe the actual work, such as **CSV Report Export and Validation**. Each exchange's title and father model are saved in `runs/<task-id>/session.json`. Empty conversations show **New session** until the first ask is named.

Older unnamed sessions show **Awaiting father title** until the father names them from their saved request and result. This happens when the team is idle, after queued project workflows, using a short father API call on the same serial scheduler. No project work is rerun. Failed naming is attempted once per app launch; open the session and choose **Ask father to name** to retry. Pausing the scheduler also pauses this naming work.

Select a row and press **Enter**, or click it, to read all of its saved asks, answers, per-exchange costs, and session total. Choose **Continue session** to make it the active conversation and send another ask. Continuing restores its workspace and history without rerunning completed work. The current input draft is preserved. Press **Escape** to close details. Older single-request sessions remain readable and become multi-exchange conversations when continued; their original results and costs remain intact. Detailed workflow handoffs, command evidence, and file manifests stay in the individual run artifacts.

## Agent folders and durable scheduling

The default agent home is `.ehv-agent` in the folder where you start the app. Subsequent commands discover the nearest `.ehv-agent` in the current directory or its ancestors. Use `--home` on any command to select an explicit, reusable home:

```sh
ehv-agent start --home C:/agents/my-team --workspace C:/projects/my-app
ehv-agent enqueue --home C:/agents/my-team "Add CSV export with tests"
```

```text
.ehv-agent/
  config.json
  active-session.json          # selected conversation, restored on restart
  sessions/<session-id>.json   # conversation identity, workspace, and creation time
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
    session.json                  # descriptive title supplied by the father
    baseline.json
    plan.json                     # task type, routing reason, answer or selected steps
    handoffs.json
    review.json
    project/                    # staged project copy
    backups/                    # original files replaced or deleted on integration
    integration.json            # integration journal and changed-file manifest
    outcome.json
    usage.json                  # API usage and costs for this ask–answer exchange
    compactions.json            # accepted conversation summaries and size estimates
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

## Automatic compaction and token-limit recovery

Automatic compaction is enabled by default. Before a role's conversation reaches approximately `compact_threshold` input tokens (default 60,000), the app asks the same model for a concise summary of older work. It retains the original assignment verbatim, the role instructions, and normally the last two complete assistant/tool-result exchanges (one during forced recovery). Repeated compactions carry the previous summary forward. This uses a tool-free summarization request through the regular Messages API, so it also works with models that lack the newer compaction beta. It can lose older detail; agents can reread staged files, resources, and handoffs when needed.

The trigger uses an approximate text-size estimate and reported usage. A selected model's saved input limit lowers the trigger to leave context headroom. Accepted summaries are saved in `compactions.json`; a failed, truncated, empty, or oversized summary leaves the existing history intact. Thinking blocks whose signatures would be invalidated by the replaced history are removed from retained messages. Summarization never runs project tools or replays completed commands.

`max_tokens` is a per-response **output** limit. Compaction reduces **input** history, so the app also handles truncated replies separately: it retries up to `max_token_retries` times (default 2), doubling the output budget up to `max_recovery_tokens` (default 32,768) and the model's advertised output limit. An existing `max_tokens` setting above that recovery default remains the ceiling. Plain-text replies continue with their partial text in context. All tool calls from a truncated response are discarded without executing them, and the model is asked to reissue complete calls and split large edits into smaller pieces. Context-window errors can force compaction before retrying. Refusals and unrelated API errors are not retried by this recovery mechanism.

Summaries, recovery calls, and normal work share `max_turns`; recovery never creates an unbounded loop. Their usage and costs are included in the session ledger, with summarization labeled `compact`. Cancellation stops compaction as well as ordinary work. Very large original requests and repeatedly oversized tool arguments may still need to be split. Previously failed workflows are not resumed automatically; use `/retry TASK_ID` for a fresh attempt.

Open **F2 → Settings** or `/settings` to enable/disable compaction, set its threshold, and change the recovery output cap. **Save conversation** works independently of model scanning and applies to the next workflow. You can also configure these from a terminal:

```powershell
.\ehv-agent.cmd config auto_compact true
.\ehv-agent.cmd config compact_threshold 60000
.\ehv-agent.cmd config max_recovery_tokens 32768
.\ehv-agent.cmd config max_token_retries 2
```

See Claude's [compaction overview](https://platform.claude.com/docs/en/build-with-claude/compaction) and [stop-reason handling](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons) for the distinction between context management and output truncation.

## Message and session costs

Costs are estimates at first-party Messages API list prices, calculated from Claude's reported uncached input, output, five-minute/one-hour cache writes, and cache reads. Built-in rates were checked on November 5, 2026 against [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing). The estimate accounts for the documented Haiku 5.5 prompt-length threshold and US inference multiplier. The model picker shows base input/output rates when a price is known.

One ledger row represents one API response or interrupted attempt, including tool turns. The **ask–answer cost** sums all such rows for that user ask, including the father, children, rework, review, compaction, retries, and naming. The **session total** adds every ask–answer cost in the conversation once; it does not reset when a new ask starts. `runs/<task-id>/usage.json` stores both conversation and task IDs, response IDs, model, phase, token categories, saved prices, and decimal USD amounts. That exchange's total also appears in its `outcome.json`. Historical messages retain their original prices. Later naming charges update the original exchange and therefore its conversation total.

Unknown model prices, unsupported service tiers or server tool fees, and attempts with no usage returned show **Unavailable**. Interrupted streams retain observed cumulative usage and mark the estimate incomplete; a **+** marks a subtotal with incomplete or unpriced messages. Older sessions without a ledger show **Unavailable**. Process termination can prevent a stream's usage from being saved. Estimates exclude account discounts and credits and are not an invoice.

The [Usage and Cost Analytics API](https://platform.claude.com/docs/en/manage-claude/usage-cost-analytics-api) reports delayed organization aggregates using admin/analytics credentials. It cannot attribute individual Messages API responses to this app's local sessions; its session dimension is for Claude Managed Agents. The app therefore uses response usage for immediate attribution and does not request admin credentials or allocate organization totals to local messages. Use Claude Console or that API to reconcile billed organization spend separately.

For an unknown model or updated rates, edit `model_pricing` in `config.json` and reload. All five rates are required, in **USD per million tokens**; use decimal strings for exact values. An exact model ID takes precedence over a canonical model alias. For example:

```json
"model_pricing": {
  "my-model-id": {
    "input": "2",
    "output": "10",
    "cache_write_5m": "2.5",
    "cache_write_1h": "4",
    "cache_read": "0.1"
  }
}
```

## Integration and failure behavior

Project files are copied into `runs/<id>/project` before the first model call. By default the snapshot excludes `.git`, agent state, dependency directories, virtual environments, build output, `.env` files, and common private-key extensions. Symbolic links are skipped in the source; new staged symbolic links block integration. Edit `excludes` in the configuration for your project. File content changes are integrated; empty directories and permission-only changes are not tracked.

The father reviews delegated reports and changes, and can request a bounded serial coding/testing repair pass (`max_rework`, default 1). Changed files require an independent testing pass even if the initial plan selected only coding. Read-only delegated analysis needs no coding or testing pass. A failed or blocked latest specialist result prevents integration, even if the model approves. The app cannot independently prove a model's semantic assessment, so reports retain command evidence and explicit limitations.

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
