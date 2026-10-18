# STATUS: durable-approval-agent

## Progress
All planned steps are done and committed locally (branch `main`, not pushed). All servers and the
`durable-approval-agent-temporal` container are stopped. The named volume
`durable-approval-agent-temporal-data` is kept, so the Temporal history survives.

## Stages
| Stage | Status | Evidence |
| --- | --- | --- |
| Cloned | yes | `ui/` = langchain-ai/agent-chat-ui (MIT) via `git subtree add --squash` at 088908b (commits 942d30e, f737648). temporalio/ai-cookbook was read only, never copied (it has no license). |
| Runnable | yes | Temporal dev server `temporalio/temporal:1.9.1` healthy (`docs/01_temporal_dev_server.txt`); SDK hello-world 1.11 s (`docs/02_hello_temporal.txt`); langgraph dev + agent-chat-ui rendering the approval interrupt (`docs/06_ui_manager_interrupt.png`, `docs/06b_ui_approve_reject.png`) |
| Customized | yes | Corvane seed data, Temporal approval workflow, SQLite ERP, LangGraph agent and tools, policy config; UI config and branding only |
| Tested | yes | 26 pytest tests passed on the time-skipping server (`docs/03_pytest.txt`); durability demo PASS on 2 consecutive runs (`docs/04_durability_demo.txt`); live agent run through both approvals to a PO (`docs/05_agent_chat_run.txt`) |
| Published | no | Local commits only, per the rules |

## Commands to run the demo (Git Bash, from the project folder)
```bash
python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"
cp .env.example .env
scripts/temporal_server.sh start
.venv/Scripts/python -m pytest -v
.venv/Scripts/python scripts/durability_demo.py          # no LLM needed
(cd ui && npm install --legacy-peer-deps && cp .env.example .env)
scripts/dev.sh start                                     # worker, langgraph dev :2024, UI :3000
.venv/Scripts/python scripts/run_chat.py new "I need 40 spindle bearings for CNC-03 from Brightline, total 12400 dollars, charge it to Indianapolis maintenance."
.venv/Scripts/python scripts/run_chat.py resume <thread_id> approve   # manager, then again for finance
# or open http://localhost:3000/?apiUrl=http://localhost:2024&assistantId=purchase_agent&threadId=<thread_id>
scripts/dev.sh stop && scripts/temporal_server.sh stop
```

## Files added or changed
- Added (mine): `approval/{__init__,models,erp,activities,workflow,client,worker}.py`,
  `agent/{__init__,graph,tools,hello_graph}.py`, `data/generate_seed.py`, `data/cost_centers.csv`,
  `data/suppliers.csv`, `config/policy.json`, `tests/test_workflow.py`, `tests/test_erp.py`,
  `scripts/{temporal_server.sh,dev.sh,hello_temporal.py,durability_demo.py,run_chat.py}`,
  `langgraph.json`, `pyproject.toml`, `.env.example`, `.gitignore`, `.gitattributes`, `README.md`,
  `STATUS.md`, `docs/01..06*`.
- Changed in upstream `ui/`: `ui/.env.example` (assistant ID `purchase_agent`),
  `ui/src/app/layout.tsx` (title, description), `ui/src/components/thread/index.tsx` (header
  text, 2 lines). Added `ui/package-lock.json`.

## Results for the portfolio site
- The workflow survived two hard worker kills: one mid-approval, one between the ERP commit and
  the activity ack. It completed with exactly 1 PO row and 1 "PO created" notice. Two consecutive
  runs passed, ~37 s each. Source: `python scripts/durability_demo.py` -> `docs/04_durability_demo.txt`.
- The killed PO write was retried on a new worker (attempt 2), and the idempotent insert returned
  the existing PO-2026-04101 instead of a duplicate. Source: worker logs quoted in `docs/04_durability_demo.txt`.
- 26 automated tests pass, covering approve, reject, finance threshold ($5,000 and probation
  suppliers), 24 h reminder plus 72 h expiry (skipped in test time), invalid requests, ignored
  bad signals, and exactly-once PO after a simulated crash. Source: `python -m pytest -v` -> `docs/03_pytest.txt`.
- Plain-language request to manager approval interrupt with 2 LLM calls on a local CPU-only
  qwen2.5:3b: 223 s end to end (179 s and 42 s per call, shared Ollama). Source: `docs/05_agent_chat_run.txt`.
- Each approval click reaches the Temporal workflow and returns the next step in 1.2 to 1.4 s,
  with no LLM call. Source: `docs/05_agent_chat_run.txt`.

## Open issues and blockers
- qwen2.5:7b timed out twice at the 300 s client timeout on the shared CPU Ollama (other builds
  queued). I did not keep that log. I switched the default to qwen2.5:3b. Only one full LLM
  conversation was run, with no multi-prompt evaluation.
- No SSO: the chat user acts as the assigned approver. In the demo, the same thread is requester
  and approver. The `review_request` path from a second thread is untested with the LLM.
- A signal for a non-pending step is logged and ignored, not refused to the caller. Temporal
  Updates would fix that.
- The UI needs `npm install --legacy-peer-deps`: upstream pins with pnpm, and its eslint peer range
  excludes TypeScript 7. `next dev` shows an upstream module-format warning ("1 Issue" badge in
  the screenshots). `next dev` also writes `ui/AGENTS.md` on start; it is gitignored.
- History note: the rules changed mid-build to forbid AI co-author trailers, so I rewrote the
  local, unpushed commits to remove them. Trees are unchanged.
