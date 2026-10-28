from app.logging import configure_logging

# arq's CLI imports this settings module before it applies its own
# --custom-log-dict, so configuring here is the earliest hook for the worker
# process (research section 1). startup_recovery re-applies it idempotently.
configure_logging("worker")

from app.worker import WorkerSettings

__all__ = ["WorkerSettings"]

if __name__ == "__main__":
    import asyncio
    from arq.worker import Worker
    worker = Worker(settings=WorkerSettings())
    asyncio.run(worker.async_run())
