# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Public open-source release of Worktide: Windows agent, FastAPI server,
  React + Ant Design admin console, and Telegram self-service attendance bot.
- Bilingual README with architecture / punch-flow diagrams and demo screenshots.
- `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, issue/PR templates.
- GitHub Actions CI: backend dry-run smoke suite + admin production build.
- `Known Limitations` documenting current security and scaling trade-offs.

### Notes
- Licensed under AGPL-3.0.
- Agent ingest endpoints are currently unauthenticated; see
  [Known Limitations](README.md#known-limitations).
