import math
import time
from collections import OrderedDict, deque
from threading import Lock


class RateLimiter:
    """Sliding-window counter per key, in memory. limit <= 0 turns it off."""

    def __init__(self, limit: int, window: float = 60.0, max_keys: int = 10_000,
                 clock=time.monotonic):
        self.limit, self.window, self.max_keys, self.clock = limit, window, max_keys, clock
        self._hits: OrderedDict[str, deque] = OrderedDict()
        self._lock = Lock()

    def _recent(self, key: str, now: float):
        queue = self._hits.get(key)
        if queue is None:
            return None
        while queue and now - queue[0] >= self.window:
            queue.popleft()
        if not queue:
            del self._hits[key]
            return None
        return queue

    def _wait(self, queue, now: float) -> int:
        return max(1, math.ceil(self.window - (now - queue[0])))

    def blocked(self, key: str) -> int:
        """Seconds to wait if the key is over the limit, else 0. Counts nothing."""
        if self.limit <= 0:
            return 0
        with self._lock:
            now = self.clock()
            queue = self._recent(key, now)
            if queue is not None and len(queue) >= self.limit:
                return self._wait(queue, now)
        return 0

    def hit(self, key: str) -> int:
        """Count one event. Returns 0 if allowed, else the seconds to wait."""
        if self.limit <= 0:
            return 0
        with self._lock:
            now = self.clock()
            queue = self._recent(key, now)
            if queue is not None and len(queue) >= self.limit:
                return self._wait(queue, now)
            if queue is None:
                queue = self._hits[key] = deque()
            queue.append(now)
            self._hits.move_to_end(key)
            while len(self._hits) > self.max_keys:
                self._hits.popitem(last=False)
        return 0