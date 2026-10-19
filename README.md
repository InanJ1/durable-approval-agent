# durable-approval-agent

[![tests](https://github.com/InanJaggi/durable-approval-agent/actions/workflows/tests.yml/badge.svg)](https://github.com/InanJaggi/durable-approval-agent/actions/workflows/tests.yml)

A purchase-request agent for Corvane Industrial (a fictional pump and gearbox manufacturer). An
employee asks for a purchase in plain language. A LangGraph agent checks the supplier and budget,
then starts a Temporal workflow that holds the approval state: manager approval, finance approval
above $5,000, then an idempotent purchase-order write to a SQLite stand-in for the ERP. Approvers
answer inside agent-chat-ui, and each answer is sent to the workflow as a Temporal signal.

## The Corvane scenario

Corvane's purchase requests need a manager's sign-off, and finance's above a threshold. Today they
get lost when an approver is away or a system restarts. I put the waiting in a Temporal workflow,
so nothing depends on a process staying alive. The request waits for days if it has to. It sends a
reminder at 24 h and expires at 72 h. A worker crash replays from history, and a retried ERP write
cannot create a second PO. All data is synthetic: 9 cost centers and 10 suppliers from
`data/generate_seed.py` (seed 42). The data includes an export's usual mess: inconsistent supplier
names, a blank supplier status, a cost center with no manager, and one nearly out of budget.

## Results

| Check | Result | Source |
| --- | --- | --- |
| Durability demo, 2 hard worker kills | completed, 1 PO row, 1 "PO created" notice; 2 consecutive runs passed, ~37 s each | `python scripts/durability_demo.py` -> `docs/04_durability_demo.txt` |
| Test suite | 26 passed: approve, reject, finance threshold, timeout (72 h skipped), invalid, ignored signals, idempotent PO | `python -m pytest -v` -> `docs/03_pytest.txt` |
| Agent run on qwen2.5:3b (shared CPU Ollama) | 2 LLM calls, 223 s from message to approval interrupt (179 s and 42 s per call) | `python scripts/run_chat.py new "..."` -> `docs/05_agent_chat_run.txt` |
| Approval round trip (interrupt -> signal -> next step) | 1.2 s manager, 1.4 s finance, no LLM call | same file |
| Approval interrupt in agent-chat-ui | headless Chrome screenshots | `docs/06_ui_manager_interrupt.png`, `docs/06b_ui_approve_reject.png` |
| Temporal hello-world on the Docker dev server | 1.11 s start to result | `python scripts/hello_temporal.py` -> `docs/02_hello_temporal.txt` |

In the durability demo, I kill worker #1 while the workflow waits for finance. Then finance
approves with no worker running. Worker #2 replays the history and writes the PO row. I kill it
after the ERP commit but before it reports back. Temporal times out that attempt after 15 s and
runs attempt 2 on worker #3. The insert finds the existing row and returns the same PO number.

## Quickstart (Windows, Git Bash)

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"
cp .env.example .env                      # LLM_MODEL, Temporal address
scripts/temporal_server.sh start          # temporalio/temporal:1.9.1 in Docker, UI on :8233
.venv/Scripts/python -m pytest            # 26 tests, no server or LLM needed
.venv/Scripts/python scripts/durability_demo.py
(cd ui && npm install --legacy-peer-deps && cp .env.example .env)
scripts/dev.sh start                      # worker, langgraph dev on :2024, agent-chat-ui on :3000
# open http://localhost:3000/?apiUrl=http://localhost:2024&assistantId=purchase_agent
# try: "I need 40 spindle bearings for CNC-03 from Brightline, total 12400 dollars,
#       charge it to Indianapolis maintenance."
scripts/dev.sh stop && scripts/temporal_server.sh stop
```

The LLM is any OpenAI-compatible endpoint (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`); the
default is the local Ollama with `qwen2.5:3b`.

## How it works

```
agent-chat-ui --> langgraph dev (agent/graph.py) --start/signal/query--> Temporal --> worker (approval/)
                   agent: LLM + 5 tools                                               validate_request
                   prepare_review -> ask_approver                                     create_purchase_order
                   (interrupt; resume = signal)                                       send_notification
                                                                                       -> var/corvane.sqlite
```

- `agent/graph.py`: the LLM only extracts the request and calls tools (`lookup_supplier`,
  `lookup_budget`, `submit_purchase_request`, `get_request_status`, `review_request`). After a
  submit or review, routing is deterministic. The graph reads the workflow's state and raises one
  interrupt per pending step in agent-chat-ui's approve/reject format. The answer becomes a
  `submit_decision` signal. The step being decided is saved in graph state before the interrupt,
  so a late answer cannot be applied to a different step.
- `approval/workflow.py`: validate, manager, finance (above $5,000 or for a probation supplier),
  create PO, notify. Approvals wait with `wait_condition` and a timeout. The signal handler drops
  decisions for a step that is not pending, duplicates, self-approval, and anyone but the assigned
  approver, and records each dropped decision in the history. ERP activities retry up to 10 times
  with backoff. A failed notification is logged and does not block the approval.
- `approval/erp.py`: one PO per request (`UNIQUE(request_id)` plus `ON CONFLICT DO NOTHING`).
  Outbox rows are keyed by request and event. Supplier and cost-center lookup tolerates the
  export's mess ("Pellum Seal Company" finds `PELLUM SEAL CO`) and returns candidates instead of
  guessing when a name is ambiguous.
- The request ID is the workflow ID, and it is derived from the thread and request fields, so a
  repeated submit returns the running workflow instead of starting a second one.
- `config/policy.json` holds the threshold, finance approver, reminder and expiry hours.

## Built on

Upstream, used as is:
- `ui/` is [langchain-ai/agent-chat-ui](https://github.com/langchain-ai/agent-chat-ui) (MIT,
  `ui/LICENSE`), added with `git subtree add --prefix ui ... main --squash` at upstream commit
  `088908b`. The chat, tool-call view and approve/reject interrupt view are all upstream.
- [temporalio/ai-cookbook](https://github.com/temporalio/ai-cookbook) was reference only. It has
  no license, so I copied no code from it. I read its human-in-the-loop recipe to learn the pattern
  of a signal handler writing workflow state that a `wait_condition` with a timeout waits on, and
  its tests to learn the time-skipping test setup. I wrote the workflow from scratch.
- Pinned packages: `temporalio` (Temporal Python SDK), `langgraph`, `langgraph-cli[inmem]`,
  `langchain-openai`. The Temporal dev server is the official `temporalio/temporal:1.9.1` image.

Changed in `ui/` (configuration and branding only): `ui/.env.example` (assistant ID
`purchase_agent`), `ui/src/app/layout.tsx` (page title), `ui/src/components/thread/index.tsx`
(header text, two lines). Added `ui/package-lock.json` from `npm install --legacy-peer-deps`.

Built by me: `approval/` (models, erp, activities, workflow, client, worker), `agent/` (graph,
tools, hello_graph), `data/` (seed generator and CSVs), `config/policy.json`, `tests/`,
`scripts/` (temporal_server.sh, dev.sh, hello_temporal.py, durability_demo.py, run_chat.py),
`langgraph.json`, `pyproject.toml`, `.env.example`, `docs/`.

## Limitations

- No sign-in. The approver on a signal is the workflow's assigned approver, and the chat user acts
  as that person. A real deployment would take the identity from SSO.
- In the demo, one chat thread is both requester and approver. The `review_request` tool lets an
  approver open a request from another thread, but I did not run that path with the LLM.
- The LLM is slow on the shared CPU server. `qwen2.5:7b` timed out twice at the 300 s client
  timeout while other builds were queued (I did not keep that log), so the default is
  `qwen2.5:3b`. I ran one full conversation, not an evaluation over many prompts.
- A decision for a step that is not pending is recorded and ignored, not refused to the caller.
  Temporal Updates would give the caller a synchronous answer.
- SQLite stands in for the ERP, and notifications go to an outbox table, not email. Temporal is the
  single-node dev server with a SQLite file in a Docker volume.
- With npm, the UI needs `--legacy-peer-deps`. The upstream pins with pnpm, and its eslint peer
  range excludes TypeScript 7. `next dev` prints an upstream module-format warning, shown as
  "1 Issue" in the screenshots.
