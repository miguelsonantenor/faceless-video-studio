from __future__ import annotations

import asyncio
import threading

from fvs.events import EventBus


def test_thread_publish_reaches_async_subscriber() -> None:
    bus = EventBus()

    async def main() -> list[dict]:
        got: list[dict] = []
        async with bus.subscribe(lambda e: e["type"] == "job") as q:
            t = threading.Thread(target=lambda: [bus.publish({"type": "activity"}),
                                                 bus.publish({"type": "job", "n": 1})])
            t.start()
            got.append(await asyncio.wait_for(q.get(), 2))
            t.join()
        return got

    assert asyncio.run(main()) == [{"type": "job", "n": 1}]
