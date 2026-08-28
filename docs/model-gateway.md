# Model gateway

The model gateway has exactly one route. The rule-only `local` provider remains
the default and sends no code to a model service.

## Configuration

Use a named preset or a version-1 TOML file containing one `[[routes]]` table:

```toml
version = 1

[[routes]]
id = "primary"
provider = "openai-compatible"
model = "review-model"
base_url = "https://model.example/v1"
api_key_env = "MODEL_API_KEY"
region = "eu-west"
```

The only accepted route fields are `id`, `provider`, `model`, `base_url`,
`api_key_env` and `region`. Secrets come from the named environment variable,
never from TOML. Multiple routes are rejected.

## Request boundary

Before transport, the gateway:

- checks repository policy restrictions for provider, model and region;
- redacts credential assignments, bearer tokens, private keys and known route
  secrets, including decoded JSON input and tool-result envelopes;
- enforces input token estimates before **and after** redaction, output token
  estimates and response byte limits;
- requires HTTPS outside loopback and an exact allowed hostname;
- disables ambient proxy use and validates redirects through the same boundary;
- optionally requires a standards-compliant JSON object response;
- accepts provider token usage only as non-negative JSON integers;
- protects repeated transport failures with a local circuit breaker.

Complete JSON objects, arrays and JSON-encoded strings are decoded before applying
the existing text rules. Non-empty string values under credential field names
(`password`, `passwd`, `api_key`/`api-key`/`apikey`, `secret`, `token`, case-insensitive)
are masked as a whole; other values retain their types. Re-encoding preserves valid
JSON and logical Diff line breaks. Unchanged messages keep their original formatting.
Duplicate JSON fields, invalid JSON Unicode, more than 64 traversal levels, or field
names colliding after secret replacement fail before provider invocation. Model
policy checks still run before redaction and transport.

Only outbound copies are changed: stored task Diff, user-authored definitions and
handoff evidence remain intact under their existing access and retention rules.
The Studio regression runs a published model Agent with wired Diff and local-rule
tool results, checks the provider's inputs, and rereads the original persisted
artifacts. Pattern redaction is **not full DLP**: arbitrary encodings and malformed
JSON/source fragments are not a general secret-detection guarantee; ordinary text
and fragments continue to use the text patterns. Do not submit live credentials
or treat redaction as a replacement for repository policy and credential rotation.

Weighted routing, fallback, shadow traffic, GitOps promotion, distributed
capacity and budget settlement are intentionally absent. Add one only after a
measured multi-provider or multi-tenant requirement exists.
