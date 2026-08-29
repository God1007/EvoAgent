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
| PR input drift while queued | New webhook tasks persist validated base/head commit SHAs and fetch only that immutable GitHub comparison; the bounded Diff and SHA-256 are persisted before execution, and a pinned-fetch failure never falls back to the current PR | Force-pushed or deleted commits can make a task unavailable; legacy tasks created before this contract still use their stored PR Diff URL |
| Forged GitHub installation callback | Short-lived signed state with durable one-time consumption, PKCE, user-token ownership verification and non-transferable tenant binding | GitHub OAuth and PostgreSQL availability are required during setup |
| SSRF/token exfiltration | HTTPS and exact host allowlists, repository/PR URL binding, redirect revalidation, no ambient proxy | DNS/network policy still matters |
| Prompt/context injection | PR content, Repository Standards Pack and Context Pack text are data; source fields are server-labelled and byte-bounded. Only allowlisted root documents and changed-path ancestor `AGENTS.md` files can enter automatic standards, and explicit caller standards are never overwritten. Studio Playbooks require `manage`, are length/shape bounded and cannot replace server-generated port schemas, workflow routing or permissions; findings still require added-line locations, typed finite confidence and evidence gates | Model judgment remains advisory; an authorized but poor Playbook, or a truncated or malicious Spec/standards file, can still reduce review quality |
| Malicious repository archive | Evidence archives require a verified installation and pinned head revision; fetching occurs only for workflows that consume evidence; compressed/indexed bytes, member count, file count, paths, symlinks, standards bytes and output lists are bounded; stored/public evidence contains no source, while selected standards remain access-controlled task context | Python static analysis is incomplete; decompression and parsing still consume bounded worker resources |
| Model secret/source leakage | JSON-aware outbound redaction, bounded payloads/responses, env-backed keys, repository policy | Pattern redaction is not full DLP; stored source remains access-controlled, not rewritten |
| Malicious Skill | Source hash plus canonical-manifest HMAC with a strong key, import denylist, execution-time output caps, time/memory limits and mandatory container isolation outside loopback | Local-development process sandbox is not a VM |
| Untrusted tests | Archive download/extraction share the configured repair-memory bound; Proof file sets are count-bounded before host materialization; no default execution; container mode drops network, privileges and inherited secrets | Containers share the host kernel |
| Cross-tenant access | Rotatable JWT/RBAC, tenant-bound member provisioning with platform-role delegation checks, platform-only audited offboarding, atomic credential changes with immediate token/state revocation, constant-work password failures, bounded authentication concurrency, persisted-task-bound DLQ, tenant-scoped task/outbox queries, platform-only global evolution/metrics, GitHub App webhooks requiring a bound installation, credential-use binding revalidation, and durable admission | Operators must remove the previous auth secret after old sessions expire |
| Duplicate external effects | Transactional outbox, message dedupe, effect receipts, and stable comment markers matched only to the configured GitHub App bot | Loopback PAT compatibility mode cannot authenticate marker ownership |
| Secret/error disclosure | Generic correlated 5xx, redacted transport errors, message-free persisted failures and path-free public Skill inventory | Authorized sandbox output may contain repository data |
| Configuration exposure through read APIs | Studio drafts/catalog and raw task, workflow and published-definition snapshots require `manage` before lookup; readers can use allowlisted console projections; role changes are revalidated on each request | Administrators can read full configuration; authorized source and user-authored text are not automatically secret-free |
| Supply chain | Hash-locked dependencies, SHA-pinned CI Actions, digest-pinned container images, audited CycloneDX runtime inventory and secret scanning | Lock refresh, image signing and provenance are release operations |
| Data loss | PostgreSQL backup/restore drill, checksummed migrations, Redis reconstruction | Recovery evidence is only as current as the last drill |

## Execution policy

- Reviews do not execute PR code by default.
- `/v1/proofs` uses the local or private-socket container executor and has no host fallback.
- Repair tests also require `EVOAGENT_REPAIR_CONTAINER_IMAGE` and fail closed
  instead of falling back to the host.
- Dynamic Skills require a container outside loopback; use an empty Skill directory
  when a deployment has no isolated Skill runtime.
- No GitHub token, model key or host environment is injected into containers.
- Linux Skill, Repair and Proof commands explicitly run as UID/GID
  `65534:65534`, even when the client runs as root or on a non-POSIX host.
  Repair/Proof extraction uses the same identity in a matching `0700` tmpfs;
  an image's default `USER` cannot silently restore root execution.
- The trusted sleep process owns the sandbox lifetime independently of the
  controller, as a separate non-root UID/GID `65533:65533`. Skill/test code
  cannot use same-UID signals/ptrace to interfere with it and is not PID 1;
  normal cleanup is exact-name and
  automatic removal covers owner loss while the kernel/daemon runs. An absolute
  expiry label lets the next job remove a paused, expired sandbox after Docker
  resumes; runtime loss and unlabelled old-version leftovers still require
  operator reconciliation;
  see [the lifecycle boundary](operations.md#sandbox-owner-loss).

## Honest limits

EvoAgent is not a malware sandbox, DLP product, hosted model control plane or
backup service. Use microVM isolation for hostile workloads, network egress
policy for defense in depth, and provider-managed encrypted backups with tested
retention for production data.

Report security issues through [SECURITY.md](../SECURITY.md).
