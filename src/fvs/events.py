"""In-process publish/subscribe for live updates.

Worker threads publish (job progress, activity lines, settings changes); async consumers
subscribe (the web app's SSE stream, and later ``fvs_job_get`` waits). Publishing is
thread-safe and never blocks: each subscriber has its own bounded queue on its own event
loop, and a subscriber that falls behind drops its oldest events.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

Event = dict[str, Any]
QUEUE_SIZE = 1000


class _Subscriber:
    def __init__(self, loop: asyncio.AbstractEventLoop, predicate: Callable[[Event], bool]):
        self.loop = loop
        self.predicate = predicate
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=QUEUE_SIZE)

    def _put(self, event: Event) -> None:  # runs on the subscriber's loop
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(event)


class EventBus:
    def __init__(self) -> None:
        self._subs: set[_Subscriber] = set()
        self._lock = threading.Lock()

    def publish(self, event: Event) -> None:
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            if not sub.predicate(event):
                continue
            try:
                sub.loop.call_soon_threadsafe(sub._put, event)
            except RuntimeError:  # the loop has closed
                with self._lock:
                    self._subs.discard(sub)

    @asynccontextmanager
    async def subscribe(
        self, predicate: Callable[[Event], bool] | None = None
    ) -> AsyncIterator[asyncio.Queue[Event]]:
        sub = _Subscriber(asyncio.get_running_loop(), predicate or (lambda _e: True))
        with self._lock:
            self._subs.add(sub)
        try:
            yield sub.queue
        finally:
            with self._lock:
                self._subs.discard(sub)
