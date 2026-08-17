# ADR 0024: Resolve client identity only across trusted proxy hops

- Status: accepted
- Date: 2026-08-17

## Context

The admission limiter and access log previously used the socket peer. This is
safe against header spoofing, but every caller shares one bucket when a reverse
proxy terminates connections. Blindly switching to `X-Forwarded-For` would let a
direct caller rotate an arbitrary leftmost value to bypass rate limiting and
poison incident evidence. Proxy products also differ in whether they append,
overwrite, or preserve an inbound chain.

## Decision

- Forwarded addresses are ignored unless the direct socket peer belongs to a
  configured `EVOAGENT_TRUSTED_PROXY_CIDRS` network.
- Configuration accepts at most 64 canonical IPv4/IPv6 networks, rejects
  duplicates, host-bit ambiguity, and the universal `0.0.0.0/0` or `::/0`
  networks. Empty configuration preserves the socket-only behavior.
- A trusted chain contains only IP literals, at most 32 hops and 4096 bytes.
  Empty tokens, hostnames, ports, scope identifiers, or invalid addresses make
  the complete header invalid and fall back to the socket peer.
- Resolution starts at the socket peer and walks `X-Forwarded-For` from right to
  left. It may cross a hop only while the current address is trusted and stops
  immediately after selecting the first untrusted address. Values farther left
  are not part of the security decision.
- The same cached identity keys admission and populates structured access logs.
  Logs contain the resolved client, socket peer, bounded source state, and
  consumed-hop count, never the raw header. Prometheus uses fixed accepted,
  ignored, and invalid counters without address labels.

## Consequences

Clients behind a correctly configured ingress receive independent rate-limit
buckets without allowing direct callers or attacker-added prefixes to mint
identities. Invalid proxy metadata degrades availability for that peer's shared
bucket instead of weakening admission, and its fixed counter makes the rollout
problem visible.

The application cannot prove source-address authenticity. Production network
policy must ensure only the named proxies can connect with addresses inside the
trusted CIDRs, and operators must update those CIDRs when ingress topology
changes. The setting is therefore deployment configuration, not automatic cloud
proxy discovery.
