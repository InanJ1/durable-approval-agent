"""Step 1 smoke test: one workflow, one activity, against the Docker dev server.

python scripts/hello_temporal.py
"""

import asyncio
import os
import time
import uuid
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.worker import Worker


@activity.defn
async def greet(name: str) -> str:
    return f"Hello, {name}. Temporal ran this activity."


@workflow.defn
class HelloCorvane:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(greet, name, start_to_close_timeout=timedelta(seconds=10))


async def main() -> None:
    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    client = await Client.connect(address)
    wf_id = f"hello-corvane-{uuid.uuid4().hex[:8]}"
    t0 = time.perf_counter()
    async with Worker(client, task_queue="hello-corvane", workflows=[HelloCorvane], activities=[greet]):
        t1 = time.perf_counter()
        result = await client.execute_workflow(HelloCorvane.run, "Corvane", id=wf_id, task_queue="hello-corvane")
        run_s = time.perf_counter() - t1
    total_s = time.perf_counter() - t0
    events = [e async for e in client.get_workflow_handle(wf_id).fetch_history_events()]
    print(f"server:   {address}")
    print(f"workflow: {wf_id}")
    print(f"result:   {result}")
    print(f"history:  {len(events)} events")
    print(f"timing:   workflow start to result {run_s:.2f} s; with worker start and graceful shutdown {total_s:.2f} s")


if __name__ == "__main__":
    asyncio.run(main())
