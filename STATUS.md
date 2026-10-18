# STATUS: durable-approval-agent

## Progress (for resume)
- Done: git repo + ui/ subtree (agent-chat-ui 088908b); venv with pinned deps; seed data (data/*.csv);
  approval/ (models, erp, activities, workflow, client, worker); agent/ (graph, tools, hello_graph);
  langgraph.json, pyproject.toml, .env.example; scripts/temporal_server.sh; docs/01_temporal_dev_server.txt.
- Done: docs/02 hello run (1.11 s), tests 26 passed (docs/03_pytest.txt).
- Done: history rewritten without AI trailers (rules changed); LLM default qwen2.5:3b (7b timed out at 300 s x2 on shared CPU); ui npm install needs --legacy-peer-deps (upstream eslint peer range).
- Next: langgraph dev + UI smoke, durability demo, screenshot, README, commit.
