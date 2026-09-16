"""Async coordination primitives safe for reusable application singletons."""

from __future__ import annotations

import asyncio
import threading
import weakref
from asyncio import AbstractEventLoop
from typing import Self


class LoopLocalAsyncLock:
    """Keep one asyncio lock per event loop.

    FastAPI application services are created once at import time, while test
    clients (and some embedding environments) can run that same service
    instance on multiple event loops.  A native ``asyncio.Lock`` can become
    bound to the first loop that waits on it and then fail on a later loop.
    Keeping the coordination lock local to each running loop preserves the
    single-flight behavior without coupling the service lifetime to one loop.
    """

    def __init__(self) -> None:
        self._locks: weakref.WeakKeyDictionary[AbstractEventLoop, asyncio.Lock] = (
            weakref.WeakKeyDictionary()
        )
        self._registry_lock = threading.Lock()

    def _lock_for_current_loop(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        with self._registry_lock:
            lock = self._locks.get(loop)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[loop] = lock
            return lock

    async def __aenter__(self) -> Self:
        await self._lock_for_current_loop().acquire()
        return self

    async def __aexit__(self, _exc_type, _exc_value, _traceback) -> None:
        self._lock_for_current_loop().release()


__all__ = ["LoopLocalAsyncLock"]
