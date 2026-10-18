# STATUS: durable-approval-agent

## Progress (for resume)
- Done: git repo + ui/ subtree (agent-chat-ui 088908b); venv with pinned deps; seed data (data/*.csv);
  approval/ (models, erp, activities, workflow, client, worker); agent/ (graph, tools, hello_graph);
  langgraph.json, pyproject.toml, .env.example; scripts/temporal_server.sh; docs/01_temporal_dev_server.txt.
- Done: docs/02 hello run (1.11 s), tests 26 passed (docs/03_pytest.txt).
- Done: history rewritten without AI trailers (rules changed); LLM default qwen2.5:3b (7b timed out at 300 s x2 on shared CPU); ui npm install needs --legacy-peer-deps (upstream eslint peer range).
- Done: durability demo PASS twice (docs/04_durability_demo.txt, ~37 s, 2 hard kills, 1 PO row).
- Done: agent run with qwen2.5:3b, 2 LLM calls, 223 s to interrupt; resumes 1.2-1.4 s; PO created (docs/05); UI screenshots docs/06*.png.
- Next: screenshot, README, commit.
