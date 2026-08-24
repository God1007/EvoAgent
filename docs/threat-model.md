# Threat model

## Assets and boundaries

Protected assets are GitHub/model credentials, JWT/webhook secrets, private PR
content, tenant data, audit history and the worker host. Untrusted boundaries
are Internet requests, PR content, GitHub/model responses, dynamic Skills and
repository test commands.

| Threat | Current control | Residual risk |
| --- | --- | --- |
| Malformed API input | Unambiguous bounded request framing, standards-compliant JSON parsing/serialization and strict scalar validation | Endpoint schemas remain hand-maintained |
| Forged webhook/replay | HMAC verification with production secrets of at least 32 bytes, current/previous rotation overlap, bounded schema validation, delivery ID, payload digest and age window; only an exact delivery retained in PostgreSQL may finish after the window | Operators must provision high-entropy secrets and remove the previous one after old deliveries drain |
| Forged GitHub installation callback | Short-lived signed state with durable one-time consumption, PKCE, user-token ownership verification and non-transferable tenant binding | GitHub OAuth and PostgreSQL availability are required during setup |
| SSRF/token exfiltration | HTTPS and exact host allowlists, repository/PR URL binding, redirect revalidation, no ambient proxy | DNS/network policy still matters |
| Prompt injection | PR content is data; findings require added-line locations, typed finite confidence and evidence gates | Model judgment remains advisory |
| Model secret/source leakage | Redaction, bounded payloads/responses, env-backed keys, repository policy | Pattern redaction is not full DLP |
| Malicious Skill | Source hash plus canonical-manifest HMAC with a strong key, import denylist, execution-time output caps, time/memory limits and mandatory container isolation outside loopback | Local-development process sandbox is not a VM |
| Untrusted tests | Archive download/extraction share the configured repair-memory bound; Proof file sets are count-bounded before host materialization; no default execution; container mode drops network, privileges and inherited secrets | Containers share the host kernel |
| Cross-tenant access | Rotatable JWT/RBAC, tenant-bound member provisioning with platform-role delegation checks, platform-only audited offboarding, atomic credential changes with immediate token/state revocation, constant-work password failures, bounded authentication concurrency, persisted-task-bound DLQ, tenant-scoped task/outbox queries, platform-only global evolution/metrics, GitHub App webhooks requiring a bound installation, credential-use binding revalidation, and durable admission | Operators must remove the previous auth secret after old sessions expire |
| Duplicate external effects | Transactional outbox, message dedupe, effect receipts, and stable comment markers matched only to the configured GitHub App bot | Loopback PAT compatibility mode cannot authenticate marker ownership |
| Secret/error disclosure | Generic correlated 5xx, redacted transport errors, message-free persisted failures and path-free public Skill inventory | Authorized sandbox output may contain repository data |
| Supply chain | Hash-locked dependencies, SHA-pinned CI Actions, digest-pinned container images, audited CycloneDX runtime inventory and secret scanning | Lock refresh, image signing and provenance are release operations |
| Data loss | PostgreSQL backup/restore drill, checksummed migrations, Redis reconstruction | Recovery evidence is only as current as the last drill |

## Execution policy

- Reviews do not execute PR code by default.
- `/v1/proofs` uses the local container executor and has no host fallback.
- Repair tests also require `EVOAGENT_REPAIR_CONTAINER_IMAGE` and fail closed
  instead of falling back to the host.
- Dynamic Skills require a container outside loopback; use an empty Skill directory
  when a deployment has no isolated Skill runtime.
- No GitHub token, model key or host environment is injected into containers.

## Honest limits

EvoAgent is not a malware sandbox, DLP product, hosted model control plane or
backup service. Use microVM isolation for hostile workloads, network egress
policy for defense in depth, and provider-managed encrypted backups with tested
retention for production data.

Report security issues through [SECURITY.md](../SECURITY.md).
