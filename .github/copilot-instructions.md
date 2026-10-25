# Copilot instructions for my-mail-mcp

Trust these instructions; search only if something here is missing or wrong.

## What this is
Self-hosted, stateless MCP server (FastMCP 4.1.3, Streamable HTTP) exposing a mailbox over IMAP/SMTP (Yahoo by default). Small Python repo (~1000 LOC in `src`, 4 test files, 91 tests). Python >=3.11 (`.python-version` = 3.12; CI runs 3.11 and 3.12). Package manager: **uv 0.13.4** (hatchling build backend). Deps: fastmcp, imapclient, pydantic-settings, certifi. Dev: pytest, ruff.

## Build / validate (run in this order; same as CI in `.github/workflows/ci.yml`)
```
uv sync --locked
uv run --locked ruff check src tests
uv run --locked ruff format --check src tests   # fix with: uv run --locked ruff format src tests
uv run --locked pytest -q                        # ~4s, 91 tests, no network/mailbox needed
uv build
docker build -t my-mail-mcp:ci .                 # CI "container" job; docker daemon must be started first
```
- Always run `ruff check` and `ruff format --check` before finishing; CI fails on either.
- `uv sync` can fail transiently with a PyPI HTTP 500 (hatchling fetch); simply retry.
- If you change dependencies in `pyproject.toml`, run `uv lock` and commit `uv.lock` (CI uses `--locked`).
- Run server: copy `.env.example` to `.env`, fill in values, then `uv run my-mail-mcp` (port 8080, `/health` endpoint, MCP at `/mcp`). `MCP_AUTH_TOKEN` must be >=32 chars. Tests don't need `.env`.

## Layout
- `src/my_mail_mcp/server.py` – `create_server`, auth (static bearer token via `StaticTokenVerifier`; optional OAuth/JWT via `AUTH_ISSUER_URL`+`PUBLIC_BASE_URL`, both or neither), `/health`, `main()`.
- `src/my_mail_mcp/config.py` – `Settings` (pydantic-settings, env vars/`.env`, `MAIL_*`, `MCP_AUTH_TOKEN`, `PORT`).
- `src/my_mail_mcp/mail/service.py` – IMAP connection/folder handling; `operations.py` – mailbox mutations (flags, move/copy, archive/trash, delete, folders); `sending.py` – SMTP send/reply/forward.
- `src/my_mail_mcp/tools/mailbox.py`, `tools/sending.py` – MCP tool registration (`register_*_tools`) wrapping the mail layer.
- `tests/test_*.py` – pytest with fakes/mocks of IMAP/SMTP.
- `Dockerfile` (multi-stage, uv, non-root), `cloudbuild.yaml` (GCP Cloud Run deploy), `docs/` (`deployment.md`, `auth0-setup.md`), `.env.example`.

## Conventions / gotchas
- Adding a tool: implement in `mail/`, register in `tools/`, add tests, and update the tools table in `README.md`.
- Message references are folder + UID + UIDVALIDITY; stale ones must be rejected. Mutations accept 1–100 distinct UIDs and validate before changing anything. Destructive tools advertise `destructiveHint`; sending limits 20 recipients and rejects header injection.
- Email content is untrusted; keep `mask_error_details=True` and never log secrets (`SecretStr`).
- No live-mailbox tests exist; do not add tests needing network or credentials.
- Update `README.md`/`docs/` when behavior or config changes.
