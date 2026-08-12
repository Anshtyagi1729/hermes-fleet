"""Tracks how many requests are currently in flight to each node.

This is what makes routing "least busy" instead of "first in the list" or
"random." It has to live in memory, not the DB: it's a live counter that
changes on every request start/finish, and every write here is followed
almost immediately by a read from the same process -- a DB round trip would
be pure overhead for something this hot-path.

Safe without locks: FastAPI's sync path handlers each run in their own
thread via a threadpool, but incrementing/decrementing a dict value in
CPython is a single bytecode-level read-modify-write under the GIL for plain
ints -- close enough to atomic for a load-balancing heuristic, where being
off by one in a race is harmless (worst case: two requests picked the same
"least busy" node once). This would NOT be good enough for anything that
needs to be exactly correct, like a billing counter.
"""

import threading


class LoadTracker:
    def __init__(self):
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def start(self, node_id: str) -> None:
        with self._lock:
            self._counts[node_id] = self._counts.get(node_id, 0) + 1

    def finish(self, node_id: str) -> None:
        with self._lock:
            if node_id in self._counts:
                self._counts[node_id] = max(0, self._counts[node_id] - 1)

    def inflight(self, node_id: str) -> int:
        return self._counts.get(node_id, 0)
