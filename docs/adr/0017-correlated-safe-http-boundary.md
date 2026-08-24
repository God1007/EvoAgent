# ADR 0017: Correlate HTTP failures without disclosing internals

- Status: Accepted
- Date: 2026-08-17

## Context

The API previously handled unexpected POST failures by returning
`str(exception)` to the caller, while unexpected GET failures escaped the
application boundary. The default access logger also recorded the complete
request target, including query parameters. Provider errors, connection strings,
source fragments, and credentials can all appear in exception messages or query
parameters, so these behaviors turned ordinary failure handling into a possible
data-exfiltration path.

Operators still need to connect a caller's failed request to one bounded server
event without enabling verbose public errors.

## Decision

- Every request receives one correlation identifier. A caller-supplied
  `X-Request-ID` is accepted only when it is 1–64 characters from a strict
  alphanumeric/period/underscore/hyphen alphabet; otherwise the API creates a
  random 128-bit identifier. Every response returns the effective identifier.
- Request identifiers are observability labels only. They are untrusted and are
  never used for authentication, authorization, tenancy, idempotency, or audit
  identity.
- One outer exception boundary covers admission control plus every GET and POST
  handler. An unexpected pre-response exception increments the error metric,
  emits a structured event, and returns only `internal server error` and the
  request identifier. The exception message and traceback are not copied to the
  response or edge log.
- If a response has already started, the handler closes the connection instead
  of writing a second HTTP status line.
- Structured access records contain timestamp, event, request identifier,
  method, normalized path, client address, status, and byte count. Query strings
  are deliberately omitted. Internal-error records add only the bounded Python
  exception type and a message-independent code-location reference. The latter
  is standardized across operational failures by ADR 0018.
- All responses, including redirects and overload responses, receive the same
  request identifier, strict same-origin content policy and browser hardening
  headers. The `Server` header does not expose the Python interpreter version.
- Only explicit `ClientInputError` and `AccessDeniedError` messages may become
  controlled 4xx responses. An arbitrary built-in `ValueError` or
  `PermissionError` is treated as an unexpected 500, so adapters cannot leak raw
  upstream text merely by choosing a common exception class.
- Collection `limit` parameters are parsed once and bounded to 1–1000 before a
  Store query, preventing malformed or extreme values from amplifying reads.
- Proof Executor adapter exceptions use the same rule: an inconclusive proof may
  expose a bounded exception type and reference, but never the adapter exception message.
  Deliberately captured sandbox command output remains part of authorized proof
  evidence and is a separate data-retention concern.

## Consequences

A caller can report one identifier that operators can find in JSON container
logs, while ordinary 5xx responses no longer reveal provider or infrastructure
details. Query credentials also stay out of the API access log. Tests inject a
secret-bearing exception and query value and require both to be absent from the
wire and captured logs.

The edge log intentionally sacrifices exception text and traceback. Root-cause
details belong in access-controlled component telemetry and traces, correlated
by request/task identifiers, not in public-edge records. Because an upstream
identifier can be spoofed or reused, ingress deployments that require globally
unique identifiers should replace the header before forwarding it; EvoAgent's
format check is not a proof of provenance.
