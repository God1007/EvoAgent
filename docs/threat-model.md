# EvoAgent Threat Model

Scope: the EvoAgent review service, its GitHub integration, dynamic skills, and
the repair verifier. This document lists assets, trust boundaries, threats, and
the mitigations that exist in the codebase today, plus known residual risks.

## 1. Assets

| Asset | Why it matters |
| --- | --- |
| GitHub token / App installation token | Can read private code and write PRs/comments |
| LLM API keys | Billable; can be exfiltrated |
| Auth/JWT secret | Forges sessions if leaked |
| Webhook secret | Authenticates inbound webhooks |
| Task store contents | Findings, feedback, audit trail, tenant data |
| Host on which the service runs | Untrusted PR code could target it |

## 2. Trust boundaries

1. **Internet → API/webhook** — anonymous or token-bearing callers.
2. **PR content → reviewer/agents** — attacker-controlled text and code.
3. **Service → GitHub / LLM** — outbound requests carrying secrets.
4. **Service → trusted plugin** — operator-installed in-process provider code.
5. **Service → dynamic skill** — untrusted third-party reviewer code.
6. **Service → repair verifier** — execution of untrusted PR code.
7. **Tenant → tenant** — multi-tenant isolation.
8. **API/worker → remote Proof Runner → job container** — signed execution and evidence.

## 3. Threats and mitigations

| # | Threat | Mitigation (code) | Residual risk |
| --- | --- | --- | --- |
| T1 | SSRF / token exfiltration via attacker-supplied `diff_url` | `github.py` enforces HTTPS + host allowlist, rejects embedded credentials/ports, re-validates redirects and strips `Authorization` cross-host, caps response/archive size | Allowlist is GitHub-only; self-hosted GitHub Enterprise needs config |
| T2 | Prompt injection from PR content | PR content is data, never system/tool instructions; findings must map to added diff lines | LLM reviewers can still be nudged; treated as advisory, gated downstream |
| T3 | Markdown injection in PR comments | `report.py` escapes all CommonMark punctuation, neutralizes backticks/newlines, dynamic-length fences | — |
| T4 | Untrusted code execution on host | `/v1/proofs` uses a container-required `proof.executor`; production can move execution to the standalone runner trust domain. Jobs are netless, read-only root, dropped caps, `no-new-privileges`, have no injected environment, and enforce CPU/mem/PID/file/output/time limits. Auto-repair can separately require container mode | Auto-repair host fallback is for trusted repositories only; containers share a kernel, so use a microVM provider for hostile public multi-tenancy |
| T5 | Resource exhaustion (fork bomb, disk fill, output flood) | PID limit (container), file-size rlimit, bounded rolling output buffer, timeout kills the process group | Host-mode fork-bomb containment is best-effort only |
| T6 | Malicious dynamic skill | Manifest + source SHA-256 + optional HMAC signature + import denylist; runs in a restricted subprocess with timeout/memory limits and no host credentials | Subprocess isolation is not a security boundary against a determined attacker |
| T7 | Secret leakage in logs/repo | Secrets only from env; `report.py` never echoes secrets; gitleaks + pip-audit in CI | — |
| T8 | Replay / duplicate webhook | Delivery id, payload digest, PR update-time window checks | — |
| T9 | Auth bypass / privilege escalation | JWT with fixed HS256, RBAC, tenant scoping, repository authorization checks | — |
| T10 | Supply-chain (tampered deps) | Hash-locked `requirements*.lock`, `--require-hashes` installs, dependency-consistency guard test, Dependabot | Transitive lock refresh is manual after Dependabot PRs |
| T11 | Unexpected in-process plugin activation | Entry-point discovery is disabled by default; enabling it requires an explicit plugin-id allowlist; manifest API/dependencies are validated and startup is transactional | A trusted plugin has process privileges; review and pin it like any production dependency |
| T12 | Source/API-key leakage through model traffic or errors | Gateway redacts common assignment/Bearer/private-key forms before transport, strips configured credentials from upstream errors, validates exact route host + HTTPS, caps response size/tokens, stores route secrets only via environment references, and persists metadata/hash rather than prompts or responses | Pattern redaction is not a complete DLP system; DNS/network policy and provider retention remain deployment responsibilities |
| T13 | Forged, replayed, redirected, or altered remote proof | Canonical request/input/evidence hashes, bidirectional HMAC-SHA256, body-bound key IDs, current/previous-key rotation, UUID/timestamp gates, pluggable Redis atomic cross-replica nonce claims, exact host allowlist, HTTPS outside loopback, disabled redirects/proxies, and bounded bodies | Redis durability/availability and rotation timing are operator responsibilities; memory replay mode is single-replica only |
| T14 | Proof evidence deletion or mutation | `ProofArtifactStorePort` supports local append-only files or S3 Object Lock. The S3 adapter uses a digest-derived key, conditional create, full-object SHA-256, version/metadata/retention verification, and only extends existing retention; signed responses expose content hashes | Object Lock protects object versions, not the AWS account, bucket policy, replication posture, or expired retention. Production still requires independent IAM/bucket-policy review and a provider-backed write/restore drill |
| T15 | Internal exception or query-secret disclosure at the public HTTP edge | One GET/POST exception boundary returns a generic correlated 500; only explicit client-safe error classes may expose 4xx messages; Proof Executor exceptions expose type but not message; structured edge logs include normalized path and bounded exception type but omit query strings, exception messages, and tracebacks; all responses carry a validated/generated `X-Request-ID` | Reviewed 4xx and sandbox command output remain visible to authorized callers; downstream component telemetry must enforce its own redaction and access control |
| T16 | Secret/source leakage through persisted failures, readiness, plugins, or automatic tracing | Operational failures use an allowlisted `operation [type; ref]` grammar whose reference hashes only exception type and traceback code locations; application adapters sanitize Queue/DLQ, Outbox/effect, task graph, Agent, proof, plugin and readiness paths; SQLite/PostgreSQL enforce failure fields again; OpenTelemetry automatic exception recording is disabled; migration 10 replaces legacy messages | Model-ledger diagnostics and sandbox command output are separate controlled surfaces; code-location references are diagnostic fingerprints, not globally unique incident IDs |

## 4. Untrusted-execution policy

- Default configuration does **not** execute PR code (reviewers are static).
- The verifier only runs a repository test command when one is configured.
- For untrusted PRs, set `EVOAGENT_REPAIR_CONTAINER_IMAGE` and
  `EVOAGENT_REPAIR_REQUIRE_CONTAINER=true`; without an image, `require_container`
  makes the verifier refuse host execution.
- For production proof workloads, configure `EVOAGENT_PROOF_RUNNER_URL`, its
  exact allowlist and signing key, then set `EVOAGENT_PROOF_REQUIRE_REMOTE=true`.
  Keep the runner on dedicated nodes with no Store, Redis, GitHub, or LLM keys.
- No GitHub token, LLM key, or host environment is injected into the verifier.
- Plugin Profile and child Scope provide composition/lifecycle isolation only;
  they are not security sandboxes.
- `X-Request-ID` is an untrusted correlation value, never an authorization,
  tenancy, idempotency, or audit identity. A production ingress may replace it
  when globally unique identifiers are required.

## 5. Known residual risks (honest boundaries)

- Process-based skill/host isolation is **not** equivalent to a VM boundary; do
  not run arbitrary untrusted code with it in production.
- Cloud metadata endpoints are not explicitly blocked in host fallback mode;
  Proof Runner jobs always rely on container mode (`--network none`).
- The LLM reviewer's judgments are advisory and not proof; high-confidence
  claims require Proof Runner evidence.
- The built-in remote runner uses containers, shared Redis replay claims, and
  optional S3 Object Lock evidence retention. A microVM provider and independent
  cloud-policy/retention drill remain deployment hardening work for hostile or
  regulated workloads.
- Model fallback is bounded and policy-filtered, but is not an availability
  guarantee without independently provisioned provider/region capacity.

## 6. Reporting

Security issues: see [`SECURITY.md`](../SECURITY.md).
