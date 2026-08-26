"""Subprocess lifecycle helpers shared by the build and flash managers."""

import asyncio
import logging
from typing import Optional
from asyncio.subprocess import Process

logger = logging.getLogger('klipperfleet.process')


async def terminate(process: Optional[Process]) -> None:
    """Kill a subprocess and reap it, if it is still running.

    Every streaming endpoint is an async generator, so a client disconnect
    closes it mid-iteration and raises GeneratorExit at the yield. Without a
    finally that calls this, `make -j4` keeps compiling and dfu-util keeps
    writing after the browser tab is gone.
    """
    if process is None or process.returncode is not None:
        return
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning('Process %s did not exit after kill', process.pid)
