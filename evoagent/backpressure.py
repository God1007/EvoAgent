"""Admission control: rate limiting and bounded concurrency.

Under overload a server must *shed* load, not collapse. These primitives let the
API reject early with ``429``/``503`` + ``Retry-After`` instead of letting every
request pile up behind a saturated resource (queue, DB pool, CPU).

All primitives are thread-safe and degrade to no-ops when disabled (limit <= 0),
so they are safe to leave wired in with default-off configuration.
"""

import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_network,
)

IPAddress = IPv4Address | IPv6Address
IPNetwork = IPv4Network | IPv6Network


@dataclass(frozen=True)
class ClientIdentity:
    """One bounded, spoof-resistant request identity for admission and logs."""

    address: str
    peer: str
    source: str
    forwarded_hops: int = 0


class TrustedProxyResolver:
    """Resolve ``X-Forwarded-For`` only across explicitly trusted proxy hops.

    The socket peer is authoritative unless it belongs to a configured CIDR.
    Starting at that peer, the chain is consumed from right to left and stops at
    the first untrusted address. Anything further left is attacker-controlled.
    Malformed or oversized chains fail closed to the socket peer.
    """

    MAX_HEADER_BYTES = 4096
    MAX_HOPS = 32

    def __init__(self, trusted_cidrs: tuple[str, ...] = ()):
        self.networks: tuple[IPNetwork, ...] = tuple(
            ip_network(value, strict=True) for value in trusted_cidrs
        )

    @staticmethod
    def _parse_address(value: str, *, socket_peer: bool = False) -> IPAddress | None:
        candidate = value.strip()
        if socket_peer and "%" in candidate:
            # A kernel-supplied IPv6 scope identifier is not part of the address
            # and can never be supplied by an X-Forwarded-For hop.
            candidate = candidate.split("%", 1)[0]
        if not candidate or len(candidate) > 64 or "%" in candidate:
            return None
        try:
            parsed = ip_address(candidate)
        except ValueError:
            return None
        if isinstance(parsed, IPv6Address) and parsed.ipv4_mapped is not None:
            return parsed.ipv4_mapped
        return parsed

    def _trusted(self, address: IPAddress) -> bool:
        return any(
            address.version == network.version and address in network for network in self.networks
        )

    def resolve(self, peer: str, forwarded_for: str = "") -> ClientIdentity:
        raw_peer = (peer or "unknown").strip()[:128] or "unknown"
        parsed_peer = self._parse_address(raw_peer, socket_peer=True)
        canonical_peer = str(parsed_peer) if parsed_peer is not None else raw_peer
        if not forwarded_for:
            return ClientIdentity(canonical_peer, canonical_peer, "socket")
        if parsed_peer is None or not self._trusted(parsed_peer):
            return ClientIdentity(canonical_peer, canonical_peer, "ignored")
        if len(forwarded_for.encode("utf-8", errors="replace")) > self.MAX_HEADER_BYTES:
            return ClientIdentity(canonical_peer, canonical_peer, "invalid")
        raw_hops = forwarded_for.split(",")
        if not 1 <= len(raw_hops) <= self.MAX_HOPS or any(not hop.strip() for hop in raw_hops):
            return ClientIdentity(canonical_peer, canonical_peer, "invalid")
        hops = [self._parse_address(hop) for hop in raw_hops]
        if any(hop is None for hop in hops):
            return ClientIdentity(canonical_peer, canonical_peer, "invalid")

        current = parsed_peer
        consumed = 0
        for hop in reversed(hops):
            if not self._trusted(current):
                break
            # None was rejected above; this keeps the narrowing explicit for mypy.
            if hop is None:  # pragma: no cover - defensive narrowing
                break
            current = hop
            consumed += 1
        return ClientIdentity(str(current), canonical_peer, "forwarded", consumed)


class TokenBucket:
    """Classic token bucket: ``rate`` tokens/sec, up to ``burst`` in reserve."""

    def __init__(self, rate: float, burst: float):
        self.rate = float(rate)
        self.capacity = float(burst)
        self._tokens = float(burst)
        self._timestamp = time.monotonic()
        self._lock = threading.Lock()

    def allow(self, cost: float = 1.0) -> tuple[bool, float]:
        """Return ``(allowed, retry_after_seconds)``."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._timestamp
            self._timestamp = now
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            if self._tokens >= cost:
                self._tokens -= cost
                return True, 0.0
            if self.rate <= 0:
                return False, 60.0
            deficit = cost - self._tokens
            return False, deficit / self.rate


class RateLimiter:
    """Per-key token buckets (e.g. keyed by client IP or tenant) with a bounded,
    LRU-evicted key table so a flood of distinct keys cannot exhaust memory."""

    def __init__(self, rate: float, burst: float | None = None, max_keys: int = 10000):
        self.rate = float(rate)
        self.burst = float(burst if burst is not None else max(1.0, rate))
        self.enabled = self.rate > 0
        self.max_keys = max(1, max_keys)
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, float]:
        if not self.enabled:
            return True, 0.0
        # Hold the lock across allow() so a key cannot be LRU-evicted between
        # lookup and consumption (which would otherwise hand a fresh full burst
        # to a churning attacker). Buckets are cheap and allow() is O(1).
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(self.rate, self.burst)
                self._buckets[key] = bucket
            else:
                self._buckets.move_to_end(key)
            while len(self._buckets) > self.max_keys:
                self._buckets.popitem(last=False)
            return bucket.allow(1.0)


class ConcurrencyLimiter:
    """Non-blocking bounded concurrency gate. ``try_acquire`` never waits, so a
    saturated gate sheds immediately rather than queueing work in the server."""

    def __init__(self, limit: int):
        self.limit = int(limit)
        self.enabled = self.limit > 0
        self._semaphore = threading.BoundedSemaphore(self.limit) if self.enabled else None
        self._in_flight = 0
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        if not self.enabled or self._semaphore is None:
            return True
        if not self._semaphore.acquire(blocking=False):
            return False
        with self._lock:
            self._in_flight += 1
        return True

    def release(self) -> None:
        if not self.enabled or self._semaphore is None:
            return
        with self._lock:
            self._in_flight -= 1
        self._semaphore.release()

    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @contextmanager
    def guard(self):
        acquired = self.try_acquire()
        try:
            yield acquired
        finally:
            if acquired:
                self.release()
