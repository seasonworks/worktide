# Security Policy

## Supported versions

Worktide is an actively developed showcase project. Security fixes target the
latest `main`; there is no long-term-support branch.

| Version | Supported |
|---------|-----------|
| `main` (latest) | ✅ |
| older tags | ❌ |

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report privately via **GitHub Security Advisories**:
*Repository → Security → Report a vulnerability* (Private Vulnerability Reporting).

Please include affected component (agent / server / admin), reproduction steps,
and impact. You can expect an initial acknowledgement within a few days and a
status update as triage proceeds. Coordinated disclosure is appreciated.

## When reporting, never include real data

Do **not** attach or paste real employee data, Telegram tokens, production
databases, server addresses, SSH keys, or production logs. Use redacted or
synthetic data only.

## Known security limitations

These are documented design trade-offs for the project's current scale (see
[Known Limitations](README.md#known-limitations) in the README):

- **Agent ingest endpoints are unauthenticated.** `/api/v1/activity/report`,
  `/api/v1/windows/report` and `/api/v1/agent/*` accept reports without
  per-device credentials, and an unknown `machine_id` may self-register. Deploy
  the ingest API behind a trusted network boundary (private tunnel, VPN, or an
  authenticated reverse proxy) — not directly on the public internet.
- **Auto-update uses a shared-secret HMAC** for SHA-256 integrity gating, not
  asymmetric package signing. Treat it as integrity verification within a
  trusted release pipeline, not a full software-supply-chain signature.
- **Admin auth is a single shared password**, and the admin console stores its
  bearer token in `localStorage`.

## Recommended production boundary

- Terminate TLS and authenticate clients at a reverse proxy in front of the API.
- Restrict the ingest endpoints to known networks until per-device enrollment
  and signed requests are implemented.
- Keep all secrets in environment files (see `server/.env.example`); never
  commit them.
