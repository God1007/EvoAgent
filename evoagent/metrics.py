import threading
import time
from collections import defaultdict
from contextlib import contextmanager


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.counters = defaultdict(float)
        self.duration_sum = defaultdict(float)
        self.duration_count = defaultdict(int)

    def inc(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self.counters[name] += value

    @contextmanager
    def timer(self, name: str):
        started = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - started
            with self._lock:
                self.duration_sum[name] += elapsed
                self.duration_count[name] += 1

    def prometheus(self) -> str:
        with self._lock:
            lines = []
            for name, value in sorted(self.counters.items()):
                lines.extend(["# TYPE evoagent_%s counter" % name, "evoagent_%s %s" % (name, value)])
            for name, value in sorted(self.duration_sum.items()):
                lines.extend([
                    "# TYPE evoagent_%s_seconds summary" % name,
                    "evoagent_%s_seconds_sum %s" % (name, value),
                    "evoagent_%s_seconds_count %s" % (name, self.duration_count[name]),
                ])
        return "\n".join(lines) + "\n"


metrics = Metrics()

