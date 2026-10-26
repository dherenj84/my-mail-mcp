# OAuth setup with Auth0 (for ChatGPT and other OAuth clients)

This guide connects an OAuth-capable MCP client (ChatGPT is the tested one) to your
deployed server using Auth0 as the identity provider (IdP). It is a
step-by-step recipe you can repeat for your own tenant. Clients that cannot do
OAuth (VS Code, scripts) keep using the static `MCP_AUTH_TOKEN`.

## How it works

The server is only a *resource server*: it never issues tokens. Auth0 does.

1. The client calls `/mcp`, gets `401`, and reads
   `/.well-known/oauth-protected-resource/mcp`, which names your Auth0 issuer.
2. The client talks to Auth0, you log in there and approve access.
3. Auth0 issues an RS256 JWT access token whose audience is `<PUBLIC_BASE_URL>/mcp`.
4. The client sends `Authorization: Bearer <jwt>`; the server verifies signature
   (via `<issuer>/.well-known/jwks.json`), exact issuer, audience and expiry.

Your Auth0 password is never seen by the client or this server and is unrelated to
your mailbox app password.

> **Security model:** the server accepts *any* valid token for its audience, so
> whoever can sign in to the Auth0 API's connection controls the mailbox
> (including sending and permanent deletion). Keep the tenant single-user (step 7).

## Server settings

| Variable | Value |
| --- | --- |
| `PUBLIC_BASE_URL` | Public HTTPS URL of the deployment, no trailing slash, e.g. `https://my-mail-mcp-123456789.us-central1.run.app` |
| `AUTH_ISSUER_URL` | Auth0 issuer, e.g. `https://YOUR_TENANT.us.auth0.com/` — **keep the trailing slash** |

Both must be set together (or neither, which disables OAuth) and must use `https`.
Neither is secret. The Auth0 **API identifier (audience)** must equal
`<PUBLIC_BASE_URL>/mcp` exactly.

## Step 0: know your service URL first

The audience depends on the public URL, and the API identifier **cannot be changed**
after creation, so decide the URL before step 2:

- Cloud Run URL: deploy once with the static token only (override
  `_AUTH_ISSUER_URL` and `_PUBLIC_BASE_URL` to empty strings, see
  [deployment](deployment.md)), then read the URL with
  `gcloud run services describe my-mail-mcp --region REGION --format='value(status.url)'`.
  Note that the deterministic form `https://SERVICE-PROJECTNUMBER.REGION.run.app` is also valid.
- Or use a custom domain you control.

## Step 1: create an Auth0 tenant

Sign up at [auth0.com](https://auth0.com), create a tenant and note its domain
(`YOUR_TENANT.REGION.auth0.com`). Your issuer is `https://<domain>/`.

## Step 2: create the API

Dashboard → **Applications → APIs → Create API**:

- Name: `my-mail-mcp`
- Identifier: `<PUBLIC_BASE_URL>/mcp` (for example
  `https://my-mail-mcp-123456789.us-central1.run.app/mcp`; no trailing slash)
- Token profile: **Auth0**, signing algorithm: **RS256**

In the API's settings set the access policy to *Per-app authorization* for
user-delegated access, and do not grant client (machine-to-machine) access.

Also turn on **Allow Offline Access** (so Auth0 can issue refresh tokens) and
enable **Refresh Token Rotation** for this API/ChatGPT application. Without this,
ChatGPT only receives a short-lived access token and cannot renew it silently, so it
periodically asks you to re-authenticate/reconnect to the connector. With refresh
tokens, ChatGPT renews access tokens in the background and each refresh issues a new
refresh token (the old one is invalidated). Existing connections keep the old
tokens until you disconnect and reconnect the connector once.

## Step 3: set the default audience

**Settings → General → API Authorization Settings → Default Audience**: enter the same
identifier. Clients such as ChatGPT may not send an `audience` parameter; without a
default, Auth0 returns an opaque (encrypted) token that the server cannot verify.

## Step 4: enable CIMD client registration

**Settings → Advanced → Client ID Metadata Document (CIMD) Registration**: turn it
on. Leave *Dynamic CIMD Registration* off.

## Step 5: register the ChatGPT application

**Applications → Create Application → Import from URL**, using
`https://chatgpt.com/oauth/client.json`. It is created as a third-party app with
`private_key_jwt` authentication and the callback
`https://chatgpt.com/connector_platform_oauth_redirect`. Auth0 warns that it ignores
`client_uri` and `token_endpoint_auth_methods_supported`; this is harmless.

If login later fails with a redirect mismatch, ChatGPT's connector page shows the
exact callback (for example `https://chatgpt.com/connector/oauth/{callback_id}`); add
it to the application's *Allowed Callback URLs*.

## Step 6: authorize the application for the API

**APIs → my-mail-mcp → Application Access**: give the ChatGPT application
**User Access** (user-delegated). No scopes are required; none are defined.

## Step 7: set up login and lock it to yourself

Third-party apps can only use domain-level connections, otherwise login fails with
`invalid_request: no connections enabled for the client`.

1. **Authentication → Database → Username-Password-Authentication**:
   turn on **Promote Connection to Domain Level** (irreversible) and enable the
   connection for the ChatGPT application.
2. Turn on **Disable Sign Ups** for that connection.
3. Do **not** enable social connections (Google, etc.) or open sign-ups.
4. Create your own user under **User Management → Users** (the only user).
5. Strongly recommended: enable MFA (Security → Multi-factor Auth).

Connection and tenant changes can take about 30 seconds to apply.

## Step 8: configure and deploy the server

Set both variables on the server. For Cloud Run via Cloud Build, pass them as
substitutions (the defaults in `cloudbuild.yaml` belong to the repository owner and
**must** be overridden):

```sh
gcloud builds submit . --region="$MAIL_GCP_REGION" --config=cloudbuild.yaml \
  --service-account="projects/$MAIL_GCP_PROJECT/serviceAccounts/my-mail-mcp-build@$MAIL_GCP_PROJECT.iam.gserviceaccount.com" \
  --substitutions="_REGION=$MAIL_GCP_REGION,_AUTH_ISSUER_URL=https://YOUR_TENANT.us.auth0.com/,_PUBLIC_BASE_URL=https://YOUR_SERVICE_URL"
```

For a trigger, set the same substitutions in the trigger. Locally or in Docker, put
`AUTH_ISSUER_URL` and `PUBLIC_BASE_URL` in `.env`.

## Step 9: connect and verify

1. Check the metadata (it must name your issuer):
   `curl https://YOUR_SERVICE_URL/.well-known/oauth-protected-resource/mcp`
2. Confirm `curl -i https://YOUR_SERVICE_URL/mcp/` returns `401`.
3. In ChatGPT, add a custom MCP connector with URL `https://YOUR_SERVICE_URL/mcp`
   (use OAuth authentication), sign in to Auth0 and approve.
4. Ask ChatGPT to list your mail folders.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `invalid_request: no connections enabled for the client` | Promote the connection to domain level and enable it for the ChatGPT app (step 7). |
| `401` with a valid-looking token | Audience or issuer mismatch: audience must equal `<PUBLIC_BASE_URL>/mcp` exactly; issuer must keep its trailing slash. Decode the token at jwt.io to compare `iss` and `aud`. |
| Token is opaque / not a JWT (`JWE`) | Default Audience not set, or API token profile/algorithm is wrong (steps 2–3). |
| Redirect mismatch at login | Add the callback shown by ChatGPT (step 5). |
| Startup error mentioning `AUTH_ISSUER_URL` | Both variables must be set together and use `https`. |
| `access_denied` / user cannot consent | The application is not authorized for the API (step 6). |
| ChatGPT periodically asks you to re-authenticate/reconnect | Offline access and refresh token rotation are not enabled (step 2), or the connector was connected before enabling them; disconnect and reconnect once. |
| Works in ChatGPT but you changed the Cloud Run URL | The audience is tied to the URL; create a new API with the new identifier. |

## Hardening ideas

- Optionally enforce your `sub` in the server as a second layer.
- Define API scopes (read vs. write) and enforce them per tool. Today any valid token
  can send mail and permanently delete; a separate scope for `send_message`,
  `reply_message`, `forward_message` and `permanently_delete_messages` is most valuable.
- If you only use OAuth clients, remove the static `MCP_AUTH_TOKEN` fallback.

## Using another IdP

The server only needs an OIDC-style issuer that publishes JWKS at
`<issuer>/.well-known/jwks.json`, signs access tokens with RS256, and puts
`<PUBLIC_BASE_URL>/mcp` in `aud`. Any such provider works with the same two
variables; Auth0 steps 2–7 map to "create an API/resource with that audience,
register the client, restrict who can sign in". The JWKS path is currently fixed.
