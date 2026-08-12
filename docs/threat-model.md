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
4. **Service → dynamic skill** — third-party plugin code.
5. **Service → repair verifier** — execution of untrusted PR code.
6. **Tenant → tenant** — multi-tenant isolation.

## 3. Threats and mitigations

| # | Threat | Mitigation (code) | Residual risk |
| --- | --- | --- | --- |
| T1 | SSRF / token exfiltration via attacker-supplied `diff_url` | `github.py` enforces HTTPS + host allowlist, rejects embedded credentials/ports, re-validates redirects and strips `Authorization` cross-host, caps response/archive size | Allowlist is GitHub-only; self-hosted GitHub Enterprise needs config |
| T2 | Prompt injection from PR content | PR content is data, never system/tool instructions; findings must map to added diff lines | LLM reviewers can still be nudged; treated as advisory, gated downstream |
| T3 | Markdown injection in PR comments | `report.py` escapes all CommonMark punctuation, neutralizes backticks/newlines, dynamic-length fences | — |
| T4 | Untrusted code execution on host | Verifier runs tests only when configured; container mode = netless, read-only root, dropped caps, `no-new-privileges`, CPU/mem/PID/file rlimits; host fallback applies rlimits + process-group kill and is documented as trusted-repo only | Host fallback is not network-isolated; strong isolation (microVM) is future work |
| T5 | Resource exhaustion (fork bomb, disk fill, output flood) | PID limit (container), file-size rlimit, bounded rolling output buffer, timeout kills the process group | Host-mode fork-bomb containment is best-effort only |
| T6 | Malicious dynamic skill | Manifest + source SHA-256 + optional HMAC signature + import denylist; runs in a restricted subprocess with timeout/memory limits and no host credentials | Subprocess isolation is not a security boundary against a determined attacker |
| T7 | Secret leakage in logs/repo | Secrets only from env; `report.py` never echoes secrets; gitleaks + pip-audit in CI | — |
| T8 | Replay / duplicate webhook | Delivery id, payload digest, PR update-time window checks | — |
| T9 | Auth bypass / privilege escalation | JWT with fixed HS256, RBAC, tenant scoping, repository authorization checks | — |
| T10 | Supply-chain (tampered deps) | Hash-locked `requirements*.lock`, `--require-hashes` installs, dependency-consistency guard test, Dependabot | Transitive lock refresh is manual after Dependabot PRs |

## 4. Untrusted-execution policy

- Default configuration does **not** execute PR code (reviewers are static).
- The verifier only runs a repository test command when one is configured.
- For untrusted PRs, set `EVOAGENT_REPAIR_CONTAINER_IMAGE` and
  `EVOAGENT_REPAIR_REQUIRE_CONTAINER=true`; without an image, `require_container`
  makes the verifier refuse host execution.
- No GitHub token, LLM key, or host environment is injected into the verifier.

## 5. Known residual risks (honest boundaries)

- Process-based skill/host isolation is **not** equivalent to a VM boundary; do
  not run arbitrary untrusted code with it in production.
- Cloud metadata endpoints are not explicitly blocked in host fallback mode;
  rely on container mode (`--network none`) for that.
- The LLM reviewer's judgments are advisory and not proof; high-confidence
  claims require executable evidence (roadmap: Proof Runner).

## 6. Reporting

Security issues: see [`SECURITY.md`](../SECURITY.md).
