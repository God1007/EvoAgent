import threading
import time
from collections import defaultdict
from collections.abc import Callable
from contextlib import contextmanager

# Prometheus-style latency buckets (seconds). The final +Inf bucket is implicit.
DEFAULT_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)


class Metrics:
    def __init__(self, buckets: tuple[float, ...] = DEFAULT_BUCKETS):
        self._lock = threading.Lock()
        self.counters: dict[str, float] = defaultdict(float)
        self.gauges: dict[str, float] = defaultdict(float)
        self.duration_sum: dict[str, float] = defaultdict(float)
        self.duration_count: dict[str, int] = defaultdict(int)
        self._buckets = tuple(sorted(buckets))
        # name -> [count per bucket ... , +Inf count]; plus sum/count tracked separately.
        self.hist_buckets: dict[str, list[int]] = {}
        self.hist_sum: dict[str, float] = defaultdict(float)
        self.hist_count: dict[str, int] = defaultdict(int)
        # Optional callbacks that produce gauge values lazily at scrape time
        # (used for queue depth, pool usage, breaker state, etc.).
        self._gauge_sources: dict[str, Callable[[], float]] = {}

    def inc(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            self.counters[name] += value

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self.gauges[name] = value

    def add_gauge(self, name: str, delta: float) -> None:
        with self._lock:
            self.gauges[name] += delta

    def register_gauge_source(self, name: str, source: Callable[[], float]) -> None:
        """Register a zero-arg callable sampled at scrape time. Exceptions in the
        source are swallowed so a broken probe never breaks the metrics endpoint."""
        with self._lock:
            self._gauge_sources[name] = source

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            buckets = self.hist_buckets.get(name)
            if buckets is None:
                buckets = [0] * (len(self._buckets) + 1)
                self.hist_buckets[name] = buckets
            placed = False
            for index, edge in enumerate(self._buckets):
                if value <= edge:
                    buckets[index] += 1
                    placed = True
                    break
            if not placed:
                buckets[-1] += 1
            self.hist_sum[name] += value
            self.hist_count[name] += 1

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

    @contextmanager
    def latency(self, name: str):
        """Observe wall-clock duration into a histogram (for percentiles)."""
        started = time.monotonic()
        try:
            yield
        finally:
            self.observe(name, time.monotonic() - started)

    def prometheus(self) -> str:
        # Sample gauge sources OUTSIDE the lock: some (e.g. Redis queue depth) do
        # network I/O, and holding the lock across them would stall every
        # inc/observe/timer in the process during a slow scrape.
        with self._lock:
            sources = dict(self._gauge_sources)
        sampled: dict[str, float] = {}
        for name, source in sources.items():
            try:
                sampled[name] = float(source())
            except Exception:  # pragma: no cover - defensive probe isolation
                continue
        with self._lock:
            gauges = dict(self.gauges)
            gauges.update(sampled)
            lines: list[str] = []
            for name, value in sorted(self.counters.items()):
                lines.append("# TYPE evoagent_%s counter" % name)
                lines.append("evoagent_%s %s" % (name, value))
            for name, value in sorted(gauges.items()):
                lines.append("# TYPE evoagent_%s gauge" % name)
                lines.append("evoagent_%s %s" % (name, value))
            for name, value in sorted(self.duration_sum.items()):
                lines.append("# TYPE evoagent_%s_seconds summary" % name)
                lines.append("evoagent_%s_seconds_sum %s" % (name, value))
                lines.append("evoagent_%s_seconds_count %s" % (name, self.duration_count[name]))
            for name in sorted(self.hist_buckets):
                buckets = self.hist_buckets[name]
                lines.append("# TYPE evoagent_%s histogram" % name)
                cumulative = 0
                for index, edge in enumerate(self._buckets):
                    cumulative += buckets[index]
                    lines.append(
                        'evoagent_%s_bucket{le="%s"} %d' % (name, _fmt_edge(edge), cumulative)
                    )
                cumulative += buckets[-1]
                lines.append('evoagent_%s_bucket{le="+Inf"} %d' % (name, cumulative))
                lines.append("evoagent_%s_sum %s" % (name, self.hist_sum[name]))
                lines.append("evoagent_%s_count %d" % (name, self.hist_count[name]))
        return "\n".join(lines) + "\n"


def _fmt_edge(edge: float) -> str:
    # Render bucket edges without a trailing ".0" for integer-valued seconds.
    return str(int(edge)) if edge == int(edge) else str(edge)


metrics = Metrics()
