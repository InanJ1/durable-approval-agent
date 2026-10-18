"""Drive the purchase agent through the LangGraph API, the same way agent-chat-ui does.

python scripts/run_chat.py new "<message>"            start a thread
python scripts/run_chat.py say <thread_id> "<message>" continue it
python scripts/run_chat.py resume <thread_id> approve|reject ["comment"]
    (sends {"decisions": [{"type": ...}]}, the payload agent-chat-ui sends from its interrupt view)
"""

import asyncio
import json
import os
import sys
import time

from langgraph_sdk import get_client

URL = os.environ.get("LANGGRAPH_URL", "http://localhost:2024")
ASSISTANT = os.environ.get("ASSISTANT_ID", "purchase_agent")


def show(msg: dict) -> None:
    kind, content = msg["type"], msg.get("content")
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    if kind == "ai" and msg.get("tool_calls"):
        for tc in msg["tool_calls"]:
            print(f"  ai -> {tc['name']}({json.dumps(tc['args'])})")
        if content:
            print(f"  ai: {content}")
    elif kind == "tool":
        print(f"  tool[{msg.get('name')}]: {content[:300]}")
    else:
        print(f"  {kind}: {content}")


async def main(argv: list[str]) -> None:
    client = get_client(url=URL)
    mode = argv[0]
    if mode == "new":
        thread = await client.threads.create()
        thread_id, kwargs = thread["thread_id"], {"input": {"messages": [{"type": "human", "content": argv[1]}]}}
        print(f"thread {thread_id}\n  human: {argv[1]}")
    elif mode == "say":
        thread_id, kwargs = argv[1], {"input": {"messages": [{"type": "human", "content": argv[2]}]}}
        print(f"thread {thread_id}\n  human: {argv[2]}")
    else:
        thread_id = argv[1]
        decision = {"type": argv[2]} | ({"message": argv[3]} if len(argv) > 3 else {})
        kwargs = {"command": {"resume": {"decisions": [decision]}}}
        print(f"thread {thread_id}\n  approver clicks: {json.dumps(decision)}")

    before = len((await client.threads.get_state(thread_id))["values"].get("messages", [])) if mode != "new" else 1
    t0 = time.perf_counter()
    await client.runs.wait(thread_id, ASSISTANT, **kwargs)
    elapsed = time.perf_counter() - t0
    state = await client.threads.get_state(thread_id)
    for m in state["values"].get("messages", [])[before:]:
        show(m)
    interrupts = [i for t in state.get("tasks", []) for i in t.get("interrupts", [])]
    for i in interrupts:
        req = i["value"]["action_requests"][0]
        print(f"  INTERRUPT {req['name']}: {req['description']}")
    print(f"run took {elapsed:.1f} s; thread status {'interrupted' if interrupts else 'idle'}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
