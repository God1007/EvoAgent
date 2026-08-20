# Threat model

## Assets and boundaries

Protected assets are GitHub/model credentials, JWT/webhook secrets, private PR
content, tenant data, audit history and the worker host. Untrusted boundaries
are Internet requests, PR content, GitHub/model responses, dynamic Skills and
repository test commands.

| Threat | Current control | Residual risk |
| --- | --- | --- |
| Forged webhook/replay | HMAC verification, delivery ID, payload digest and age window | Secret rotation is operational |
| SSRF/token exfiltration | HTTPS and exact host allowlists, redirect revalidation, no ambient proxy | DNS/network policy still matters |
| Prompt injection | PR content is data; findings must map to added lines and pass evidence gates | Model judgment remains advisory |
| Model secret/source leakage | Redaction, bounded payloads/responses, env-backed keys, repository policy | Pattern redaction is not full DLP |
| Malicious Skill | Hash/signature checks, import denylist, time/memory/output limits, optional container | Process sandbox is not a VM |
| Untrusted tests | No default execution; container mode drops network, privileges and inherited secrets | Containers share the host kernel |
| Cross-tenant access | JWT/RBAC, tenant-scoped queries and durable admission | Operators retain aggregate visibility |
| Duplicate external effects | Transactional outbox, message dedupe, stable comment markers and effect receipts | Provider-side behavior must remain idempotent |
| Secret/error disclosure | Generic correlated 5xx, redacted transport errors, message-free persisted failures | Authorized sandbox output may contain repository data |
| Supply chain | Hash-locked dependencies, audit and secret scanning in CI | Lock refresh and image provenance are operational |
| Data loss | PostgreSQL backup/restore drill, checksummed migrations, Redis reconstruction | Recovery evidence is only as current as the last drill |

## Execution policy

- Reviews do not execute PR code by default.
- `/v1/proofs` uses the local container executor and has no host fallback.
- For untrusted repair tests, set `EVOAGENT_REPAIR_CONTAINER_IMAGE` and
  `EVOAGENT_REPAIR_REQUIRE_CONTAINER=true`.
- Dynamic Skills should require a container for hostile public inputs.
- No GitHub token, model key or host environment is injected into containers.

## Honest limits

EvoAgent is not a malware sandbox, DLP product, hosted model control plane or
backup service. Use microVM isolation for hostile workloads, network egress
policy for defense in depth, and provider-managed encrypted backups with tested
retention for production data.

Report security issues through [SECURITY.md](../SECURITY.md).
