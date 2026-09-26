"""Thread-safe internal event bus: producers (streams, scheduler) put events, the daemon loop drains them in order."""
from __future__ import annotations

import queue
import threading
from typing import Any, Callable


class EventBus:
    def __init__(self, maxsize: int = 100_000):
        self.q: queue.Queue = queue.Queue(maxsize=maxsize)
        self.subscribers: dict[type, list[Callable[[Any], None]]] = {}
        self.dropped = 0
        self._lock = threading.Lock()

    def publish(self, event: Any) -> bool:
        try:
            self.q.put_nowait(event)
            return True
        except queue.Full:
            with self._lock:
                self.dropped += 1
            return False

    def subscribe(self, kind: type, fn: Callable[[Any], None]) -> None:
        self.subscribers.setdefault(kind, []).append(fn)

    def poll(self, timeout: float = 1.0) -> Any | None:
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self, limit: int = 10_000) -> list[Any]:
        out = []
        while len(out) < limit:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                break
        return out

    def dispatch(self, event: Any) -> int:
        n = 0
        for kind, fns in self.subscribers.items():
            if isinstance(event, kind):
                for fn in fns:
                    fn(event); n += 1
        return n

    def __len__(self) -> int:
        return self.q.qsize()
