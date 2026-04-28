"""PersonalMem capture daemon — async loop that owns the AX watcher
subprocess and writes captures to ``~/.personalmem/``.

Stripped down from OpenChronicle: no session manager, no time-window
reducer, no classifier, no MCP server. PersonalMem's thread-routing /
summarization stages run as separate offline replays via the CLI.
"""

from __future__ import annotations

import asyncio
import os
import signal
from contextlib import suppress

from . import logger as logger_mod
from . import paths
from .capture import scheduler as capture_scheduler
from .config import Config

logger = logger_mod.get("personalmem.daemon")


async def _run(cfg: Config) -> None:
    paths.ensure_dirs()
    # Initialize file-based loggers so we get diagnostics in daemon mode
    # (stderr is /dev/null after the double-fork). Call once at startup.
    logger_mod.setup(console=False)
    paths.pid_file().write_text(str(os.getpid()))

    tasks: list[asyncio.Task] = [
        asyncio.create_task(
            capture_scheduler.run_forever(cfg.capture),
            name="capture",
        ),
    ]

    stop = asyncio.Event()

    def _handle_stop() -> None:
        logger.info("shutdown signal received")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _handle_stop)

    done_task = asyncio.create_task(stop.wait())
    await asyncio.wait(
        [done_task, *tasks], return_when=asyncio.FIRST_COMPLETED
    )

    for t in tasks:
        t.cancel()
    with suppress(asyncio.CancelledError):
        await asyncio.gather(*tasks, return_exceptions=True)

    with suppress(FileNotFoundError):
        paths.pid_file().unlink()
    logger.info("daemon stopped")


def run(cfg: Config) -> None:
    asyncio.run(_run(cfg))
