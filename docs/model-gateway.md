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
- redacts credential assignments, bearer tokens, private keys and the active
  API key;
- enforces input/output token estimates and response byte limits;
- requires HTTPS outside loopback and an exact allowed hostname;
- disables ambient proxy use and validates redirects through the same boundary;
- optionally requires a JSON object response;
- protects repeated transport failures with a local circuit breaker.

Weighted routing, fallback, shadow traffic, GitOps promotion, distributed
capacity and budget settlement are intentionally absent. Add one only after a
measured multi-provider or multi-tenant requirement exists.
