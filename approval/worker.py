"""Run the Temporal worker for Corvane purchase approvals.

python -m approval.worker
"""

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from temporalio.worker import Worker

from approval import erp
from approval.activities import create_purchase_order, send_notification, validate_request
from approval.client import connect
from approval.workflow import TASK_QUEUE, PurchaseApprovalWorkflow


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s worker[%(process)d] %(message)s", datefmt="%H:%M:%S")
    with erp.session():  # create the ERP tables and load master data once
        pass
    client = await connect()
    with ThreadPoolExecutor(max_workers=4) as pool:
        worker = Worker(
            client, task_queue=TASK_QUEUE, workflows=[PurchaseApprovalWorkflow],
            activities=[validate_request, create_purchase_order, send_notification],
            activity_executor=pool,
        )
        logging.info("worker started on %s (pid %d, ERP %s)", TASK_QUEUE, os.getpid(), erp.db_path())
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
