# Auth0 OAuth setup for ChatGPT

Records the Auth0 tenant configuration prepared for connecting ChatGPT to this
server over OAuth. It follows step 2 of the
[openai-mcpkit authenticated scaffold](https://github.com/openai/openai-mcpkit/blob/main/python-authenticated-mcp-server-scaffold/README.md#2-configure-auth0-authentication).

> **Status:** the Auth0 tenant is configured and the server validates Auth0 JWTs
> (when `AUTH_ISSUER_URL` and `PUBLIC_BASE_URL` are set) alongside the static
> `MCP_AUTH_TOKEN`. Verified end to end: ChatGPT connected through Auth0 and
> reached the server. See [Remaining work](#remaining-work).

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

6. **Login connection** (needed or the login fails with `invalid_request: no
   connections enabled for the client`): third-party apps can only use
   domain-level connections. On `Username-Password-Authentication`, *Promote
   Connection to Domain Level* is on (irreversible) and the connection is enabled for
   the ChatGPT app.
7. **Single owner:** *Disable Sign Ups* is on for that connection, Google is not
   enabled, and the only user is the owner's account. The server accepts any valid
   token for its audience, so whoever can log in owns the mailbox. Do not enable
   Google or open signups without also checking the user's identity in the server.
   A new user can only be added from the Auth0 dashboard.

## How the connection works

ChatGPT never creates tokens; Auth0 issues them.

1. ChatGPT calls `/mcp`, gets `401`, and reads
   `/.well-known/oauth-protected-resource/mcp`, which names the Auth0 issuer.
2. ChatGPT reads Auth0's authorization-server metadata and uses
   `client_id=https://chatgpt.com/oauth/client.json` (CIMD), which Auth0 matches to the
   imported ChatGPT application.
3. The owner logs in on Auth0's page (the only place the username and password are
   used) and approves the consent screen. Auth0 redirects back with a code.
4. ChatGPT exchanges the code, authenticating with `private_key_jwt` against
   `https://chatgpt.com/oauth/jwks.json`, and receives an RS256 access token whose
   audience is the API identifier.
5. ChatGPT sends `Authorization: Bearer <token>`; the server verifies signature,
   issuer, audience and expiry.

The username and password are never given to ChatGPT or the MCP server, and are
unrelated to the mailbox password in Secret Manager.

Sources: MCP authorization spec (OAuth 2.1, RFC 9728, RFC 8707, CIMD), OpenAI's
authentication guide for MCP servers, and the openai-mcpkit README.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Auth0 error `invalid_request: no connections enabled for the client` | Third-party apps need a domain-level connection. Promote the connection and enable it for the ChatGPT app (item 6 above). |
| `401` from `/mcp` with a valid-looking token | Audience or issuer mismatch. The audience must equal `<PUBLIC_BASE_URL>/mcp` exactly and the issuer must keep its trailing slash. |
| Redirect mismatch at login | ChatGPT's connector page may show `https://chatgpt.com/connector/oauth/{callback_id}`; add it to the ChatGPT application's callbacks. |
| Startup fails mentioning `AUTH_ISSUER_URL` | Both OAuth variables must be set together and use `https`. |
| Changes in Auth0 not visible yet | Connection and tenant changes can take about 30 seconds. |

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

- Consider enabling MFA for the owner user.
- Optionally enforce the owner's `sub` in the server as a second layer.
- Optionally define scopes (for example read vs. write mail tools) on the API, add
  them to the ChatGPT grant and enforce them per tool. Today any valid token for the
  audience can use every tool.
- If you only use ChatGPT, consider removing the static `MCP_AUTH_TOKEN` fallback so
  Auth0 is the only way in.
