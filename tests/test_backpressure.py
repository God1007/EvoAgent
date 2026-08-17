import time
import unittest

from evoagent.backpressure import (
    ConcurrencyLimiter,
    RateLimiter,
    TokenBucket,
    TrustedProxyResolver,
)


class TrustedProxyResolverTests(unittest.TestCase):
    def test_untrusted_peer_cannot_spoof_forwarded_identity(self):
        identity = TrustedProxyResolver(("10.0.0.0/8",)).resolve("198.51.100.7", "203.0.113.9")

        self.assertEqual("198.51.100.7", identity.address)
        self.assertEqual("ignored", identity.source)
        self.assertEqual(0, identity.forwarded_hops)

    def test_chain_walks_from_right_and_stops_at_first_untrusted_hop(self):
        identity = TrustedProxyResolver(("127.0.0.1/32", "10.0.0.0/8")).resolve(
            "127.0.0.1",
            "203.0.113.99, 198.51.100.4, 10.2.3.4",
        )

        self.assertEqual("198.51.100.4", identity.address)
        self.assertEqual("127.0.0.1", identity.peer)
        self.assertEqual("forwarded", identity.source)
        self.assertEqual(2, identity.forwarded_hops)

    def test_malformed_or_oversized_trusted_chain_fails_closed(self):
        resolver = TrustedProxyResolver(("127.0.0.1/32",))

        for value in (
            "198.51.100.8:443",
            "198.51.100.8,,10.0.0.1",
            "proxy.internal",
            "1" * (resolver.MAX_HEADER_BYTES + 1),
            ",".join("10.0.0.%d" % index for index in range(resolver.MAX_HOPS + 1)),
        ):
            with self.subTest(value=value[:40]):
                identity = resolver.resolve("127.0.0.1", value)
                self.assertEqual("127.0.0.1", identity.address)
                self.assertEqual("invalid", identity.source)

    def test_ipv4_mapped_socket_peer_uses_the_ipv4_trust_policy(self):
        identity = TrustedProxyResolver(("127.0.0.0/8",)).resolve(
            "::ffff:127.0.0.1", "198.51.100.10"
        )

        self.assertEqual("198.51.100.10", identity.address)
        self.assertEqual("127.0.0.1", identity.peer)


class TokenBucketTests(unittest.TestCase):
    def test_burst_then_denied(self):
        bucket = TokenBucket(rate=100, burst=2)
        self.assertTrue(bucket.allow()[0])
        self.assertTrue(bucket.allow()[0])
        allowed, retry_after = bucket.allow()
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0.0)

    def test_refills_over_time(self):
        bucket = TokenBucket(rate=100, burst=1)
        self.assertTrue(bucket.allow()[0])
        self.assertFalse(bucket.allow()[0])
        time.sleep(0.05)  # ~5 tokens refilled at 100/s
        self.assertTrue(bucket.allow()[0])

    def test_zero_rate_reports_retry_after(self):
        bucket = TokenBucket(rate=0, burst=1)
        self.assertTrue(bucket.allow()[0])
        allowed, retry_after = bucket.allow()
        self.assertFalse(allowed)
        self.assertEqual(60.0, retry_after)


class RateLimiterTests(unittest.TestCase):
    def test_disabled_when_rate_zero(self):
        limiter = RateLimiter(rate=0)
        self.assertFalse(limiter.enabled)
        for _ in range(1000):
            self.assertTrue(limiter.check("any")[0])

    def test_keys_are_independent(self):
        limiter = RateLimiter(rate=1, burst=1)
        self.assertTrue(limiter.check("a")[0])
        self.assertFalse(limiter.check("a")[0])
        self.assertTrue(limiter.check("b")[0])  # separate bucket

    def test_key_table_is_bounded(self):
        limiter = RateLimiter(rate=1, burst=1, max_keys=10)
        for i in range(50):
            limiter.check("k%d" % i)
        self.assertLessEqual(len(limiter._buckets), 10)


class ConcurrencyLimiterTests(unittest.TestCase):
    def test_bounded_acquire_and_release(self):
        gate = ConcurrencyLimiter(limit=2)
        self.assertTrue(gate.try_acquire())
        self.assertTrue(gate.try_acquire())
        self.assertFalse(gate.try_acquire())
        self.assertEqual(2, gate.in_flight())
        gate.release()
        self.assertEqual(1, gate.in_flight())
        self.assertTrue(gate.try_acquire())

    def test_disabled_is_always_admitted(self):
        gate = ConcurrencyLimiter(limit=0)
        self.assertFalse(gate.enabled)
        for _ in range(100):
            self.assertTrue(gate.try_acquire())
        self.assertEqual(0, gate.in_flight())

    def test_guard_releases_on_exit(self):
        gate = ConcurrencyLimiter(limit=1)
        with gate.guard() as admitted:
            self.assertTrue(admitted)
            self.assertFalse(gate.try_acquire())
        self.assertTrue(gate.try_acquire())


if __name__ == "__main__":
    unittest.main()
