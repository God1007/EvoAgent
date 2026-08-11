# Security Policy

## Supported versions

Security fixes are applied to the latest version on the `main` branch.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Do not open a public
issue containing exploit details, secrets, private repository data, or personally identifiable
information.

Include the affected component, reproduction steps, impact, and a minimal proof of concept when
possible. The maintainer should acknowledge a report within seven days and coordinate disclosure
after a fix is available.

## Security boundaries

Dynamic Skills are untrusted inputs. They must retain checksum/signature validation, isolated
execution, resource limits, and blocked host permissions. Changes to authentication, webhook
verification, tenant isolation, repair execution, or rollout gates require explicit regression
tests and security review.

