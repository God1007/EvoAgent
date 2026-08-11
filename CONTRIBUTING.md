# Contributing to EvoAgent

Thanks for improving EvoAgent. Changes should be small, reviewable, and backed by evidence.

## Development setup

EvoAgent supports Python 3.11 and 3.12. Create a virtual environment, then install the
locked development environment:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install "pip<26"
make install
pre-commit install
pre-commit install --hook-type pre-push
```

## Quality contract

Before opening a pull request, run:

```bash
make check
```

Every behavior change must include or update tests. Bug fixes should first demonstrate the
failure and then prove the fix. Do not lower coverage, lint, type, or security gates to make a
change pass. Generated lock files must be refreshed with `make lock` when dependencies change.

## Pull requests

- Explain the problem, design choice, risk, and verification evidence.
- Keep unrelated refactors out of the same pull request.
- Never commit credentials, tokens, private keys, production data, or unredacted prompts.
- For evolution changes, report validation and holdout metrics separately and preserve the
  existing rollback path.
- Record durable architectural decisions in `docs/adr/`.

