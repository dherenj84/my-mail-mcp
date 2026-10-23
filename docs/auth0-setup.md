# Auth0 OAuth setup for ChatGPT

Records the Auth0 tenant configuration prepared for connecting ChatGPT to this
server over OAuth. It follows step 2 of the
[openai-mcpkit authenticated scaffold](https://github.com/openai/openai-mcpkit/blob/main/python-authenticated-mcp-server-scaffold/README.md#2-configure-auth0-authentication).

> **Status:** the Auth0 tenant is configured and the server validates Auth0 JWTs
> (when `AUTH_ISSUER_URL` and `PUBLIC_BASE_URL` are set) alongside the static
> `MCP_AUTH_TOKEN`. Not yet tested against the live tenant or ChatGPT; see
> [Remaining work](#remaining-work).

## Values

| Item | Value |
| --- | --- |
| Tenant | `dev-fh3rg5w3wwzzwzgs` (US region, Development) |
| Issuer | `https://dev-fh3rg5w3wwzzwzgs.us.auth0.com/` (trailing slash matters) |
| JWKS | `https://dev-fh3rg5w3wwzzwzgs.us.auth0.com/.well-known/jwks.json` |
| API name | `my-mail-mcp` |
| API identifier (audience) | `https://my-mail-mcp-1080363999711.us-central1.run.app/mcp` |
| Token profile / algorithm | Auth0 / RS256 |
| MCP client | ChatGPT, CIMD `https://chatgpt.com/oauth/client.json` |

The API identifier cannot be changed after creation. It is the MCP endpoint without
a trailing slash; the server also serves `/mcp/`, so make the audience check match
what the client sends.

## What was configured

1. **API** `my-mail-mcp` created with the identifier above (Auth0 profile, RS256).
   Access policy is *Per-app authorization* for user-delegated and client access.
2. **Default audience** (Tenant Settings → General) set to the API identifier, so
   Auth0 issues an unencrypted RS256 JWT for it.
3. **CIMD registration** enabled (Settings → Advanced → *Client ID Metadata Document
   (CIMD) Registration*). *Dynamic CIMD Registration* is left off.
4. **ChatGPT client** imported (Applications → Create Application → Import from URL)
   from the CIMD URL above. It is a third-party app using `private_key_jwt` with
   ChatGPT's published JWKS, and the stable callback
   `https://chatgpt.com/connector_platform_oauth_redirect`. Auth0 warns it ignores
   `client_uri` and `token_endpoint_auth_methods_supported`; this is harmless.
5. **Grant**: ChatGPT has *User-Delegated Access* to the API (APIs → `my-mail-mcp` →
   Application Access). Client (machine-to-machine) access is not granted.

No scopes are defined on the API, so the grant carries none.

## Server behavior and environment variables

Neither value is a secret. Both must be set to enable OAuth; setting only one fails
startup.

```text
AUTH_ISSUER_URL=https://dev-fh3rg5w3wwzzwzgs.us.auth0.com/
PUBLIC_BASE_URL=https://my-mail-mcp-1080363999711.us-central1.run.app
```

The server (`build_auth` in `src/my_mail_mcp/server.py`) accepts either:

- an Auth0 RS256 JWT, verified against `<issuer>/.well-known/jwks.json` with exact
  issuer match and audience `<PUBLIC_BASE_URL>/mcp`; or
- the static `MCP_AUTH_TOKEN` (kept for VS Code and other clients).

It publishes RFC 9728 metadata at `/.well-known/oauth-protected-resource/mcp`
pointing clients at the Auth0 issuer. `cloudbuild.yaml` passes both variables to
Cloud Run through the `_AUTH_ISSUER_URL` and `_PUBLIC_BASE_URL` substitutions.

## Remaining work

- Verify against the live tenant: deploy, then add the connector in ChatGPT and
  complete the login. Tests use a locally generated key, not Auth0's.
- Optionally define scopes (for example read vs. write mail tools) on the API, add
  them to the ChatGPT grant and enforce them per tool. Today any valid token for the
  audience can use every tool.
- If ChatGPT's connector page shows a callback of the form
  `https://chatgpt.com/connector/oauth/{callback_id}`, add it to the ChatGPT
  application's callbacks.
- Cloud Run already deploys with `--no-invoker-iam-check`, which ChatGPT needs.
