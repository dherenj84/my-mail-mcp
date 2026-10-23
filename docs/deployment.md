# Cloud Build → Cloud Run

`cloudbuild.yaml` follows Google's [Cloud Build deployment guide](https://docs.cloud.google.com/build/docs/deploying-builds/deploy-cloud-run):
run checks, build the Dockerfile, push to Artifact Registry, then deploy Cloud Run.
The image uses the unique Cloud Build `$BUILD_ID`, which also works for manual
builds without a Git commit SHA. Steps run sequentially and stop on failure.
The `images` field records the artifact; the explicit push makes it available
before deployment. Nothing is deployed until you submit a build or run a trigger.

## One-time GCP setup

Use a billing-enabled GCP project and install/authenticate the Google Cloud CLI.
The following commands are examples to run in your own project, not provisioning
that has already been performed:

```sh
export MAIL_GCP_PROJECT=your-project-id
export MAIL_GCP_REGION=us-central1

gcloud services enable cloudbuild.googleapis.com run.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com iam.googleapis.com cloudresourcemanager.googleapis.com logging.googleapis.com --project="$MAIL_GCP_PROJECT"

gcloud artifacts repositories create my-mail-mcp --repository-format=docker --location="$MAIL_GCP_REGION" --project="$MAIL_GCP_PROJECT"
gcloud iam service-accounts create my-mail-mcp-build --project="$MAIL_GCP_PROJECT"
gcloud iam service-accounts create my-mail-mcp-runtime --project="$MAIL_GCP_PROJECT"
```

Separate the [build identity](https://docs.cloud.google.com/build/docs/securing-builds/configure-user-specified-service-accounts)
from the Cloud Run runtime identity. Configure permissions for the chosen accounts:

| Principal | Permission/role | Scope |
| --- | --- | --- |
| Build service account | Artifact Registry Writer (`roles/artifactregistry.writer`) | Target Artifact Registry repository |
| Build service account | Cloud Run Admin (`roles/run.admin`) | Deployment project; supports initial service creation and Invoker IAM setting |
| Build service account | Service Account User (`roles/iam.serviceAccountUser`) | Runtime service account only |
| Build service account | Logs Writer (`roles/logging.logWriter`) | Build project |
| Build service account | Storage Object Viewer (`roles/storage.objectViewer`) | Source-upload bucket, for manual builds |
| Runtime service account | Secret Manager Secret Accessor (`roles/secretmanager.secretAccessor`) | Each of the three required secrets |
| Person configuring/submitting builds | Cloud Build Editor (`roles/cloudbuild.builds.editor`) and Service Account User | Build project / chosen build service account |

For source connections and trigger creation, the administrator also needs the
relevant repository-connection and trigger permissions. Enable APIs and create
resources with an appropriately privileged setup identity. Do not grant broad
project Editor permissions to either service account. The build does not read
mail credentials; the runtime identity needs no build or deployment roles.

Cloud Logging is explicitly selected because user-managed build accounts require
a supported log destination. A manual source upload can require permission on
Cloud Build's staging bucket; create the bucket first or have your administrator
configure a dedicated source bucket and grant the build account read access.

## Runtime secrets

Create these Secret Manager secrets and add a value to each:

| Secret | Exact value |
| --- | --- |
| `my-mail-mcp-username` | Full mailbox login, for example your Yahoo address |
| `my-mail-mcp-password` | Yahoo app password |
| `my-mail-mcp-token` | Random MCP bearer token, at least 32 characters |

Use the Secret Manager console, or `gcloud secrets create` followed by
`gcloud secrets versions add --data-file=/secure/path/to/value-file`. Value files
should contain the exact value without a trailing newline and stay outside the
repository. Never place actual secrets in build substitutions, Git, Docker build
arguments, or this YAML. Add the runtime account's Secret Accessor binding to each
secret. See [Google's secret configuration guide](https://docs.cloud.google.com/run/docs/configuring/services/secrets).

If you created these secrets with placeholder values, replace them before
the first deployment: startup rejects known placeholder
credentials, so a public instance cannot run with those example values.

The build passes only secret references to Cloud Run with `--update-secrets`.
The references use `latest`, so adding a new secret version does not require
editing this configuration. Cloud Run resolves environment-variable secrets at
instance startup; existing instances retain their current values. Deploy a new
revision after rotation to move all serving instances onto the new values. This
is startup refresh, not per-request secret retrieval. Other credentials and
configuration already on the service are retained by the update flags.
If you previously configured these same keys as plain environment variables,
remove those conflicting entries before switching them to secret references.

## Run a build manually

After provisioning the repository, accounts, secrets, and IAM bindings:

```sh
gcloud builds submit . \
  --project="$MAIL_GCP_PROJECT" \
  --region="$MAIL_GCP_REGION" \
  --config=cloudbuild.yaml \
  --service-account="projects/$MAIL_GCP_PROJECT/serviceAccounts/my-mail-mcp-build@$MAIL_GCP_PROJECT.iam.gserviceaccount.com" \
  --substitutions="_REGION=$MAIL_GCP_REGION"
```

`.gcloudignore` excludes local credentials, editor configuration, virtual
environments and build artifacts from uploaded source. `.dockerignore` separately
controls what enters the Docker build context. Review uploads before submitting:

```sh
gcloud meta list-files-for-upload
```

This is a deployment command: a successful build deploys a revision and directs
traffic to it. It can incur build, storage, logging, secret and Cloud Run charges.

## Continuous deployment from GitHub

1. Push this project to `dherenj84/my-mail-mcp` when ready.
2. Connect the repository using Cloud Build's GitHub connection workflow.
3. Create a push trigger for `^main$`, selecting **Cloud Build configuration file**
   at `/cloudbuild.yaml` and the dedicated **build** service account.
   Under **Ignored files filter (glob)**, add `docs/**`, `**/*.md` and `LICENSE` so
   documentation-only pushes do not build or deploy. A push that also changes other
   files still builds. As a one-off, put `[skip ci]` in the commit message.
4. Set the substitutions listed below for your project and mailbox. The runtime
   identity is set by the YAML; selecting a trigger account does not replace it.
5. Run the trigger once and verify health and MCP authentication before normal use.

Do not use this deployment config as a pull-request trigger for untrusted code.
Use the separate GitHub CI workflow for pull requests. Cloud Build does not
inherit GitHub Actions environment variables or secrets. Set substitutions in
the Cloud Build trigger and store runtime credentials in GCP Secret Manager.
A Cloud Run repository integration must be configured to use this build file;
its generated default pipeline may otherwise bypass these checks and settings.

## Substitutions

| Key | Default | Purpose |
| --- | --- | --- |
| `_REGION` | `us-central1` | Artifact Registry location and Cloud Run region |
| `_REPOSITORY` | `my-mail-mcp` | Existing Docker Artifact Registry repository |
| `_SERVICE` | `my-mail-mcp` | Cloud Run service name |
| `_RUNTIME_SERVICE_ACCOUNT` | `my-mail-mcp-runtime@PROJECT_ID.iam.gserviceaccount.com` | Runtime identity; PROJECT_ID resolves automatically |
| `_MAX_INSTANCES` | `3` | Cloud Run service maximum instance setting |
| `_MAIL_IMAP_HOST` | `imap.mail.yahoo.com` | Provider's IMAP host |
| `_MAIL_IMAP_PORT` | `993` | Provider's implicit TLS port |
| `_AUTH_ISSUER_URL` | Auth0 tenant URL | OAuth issuer; see [Auth0 setup](auth0-setup.md) |
| `_PUBLIC_BASE_URL` | Cloud Run service URL | Used to build the `/mcp` token audience |
| `_MAIL_USERNAME_SECRET` | `my-mail-mcp-username:latest` | Secret name and version |
| `_MAIL_PASSWORD_SECRET` | `my-mail-mcp-password:latest` | Secret name and version |
| `_MCP_AUTH_TOKEN_SECRET` | `my-mail-mcp-token:latest` | Secret name and version |

Cloud Run supplies `PORT`; do not configure it as a regular environment variable.
The service scales down to zero with a maximum setting of three by default,
uses one CPU / 512 MiB per instance, and accepts up to ten simultaneous requests
per instance. Adjust these explicit deployment settings for your workload.
Optional `MAIL_ARCHIVE_FOLDER`, `MAIL_TRASH_FOLDER` and `MAIL_SENT_FOLDER` overrides can be configured
on Cloud Run and are preserved across builds. Other compatible providers can
supply different host, port, and credential references without source changes.

The `send_message`, `reply_message` and `forward_message` tools use the same
mailbox credentials over SMTP (`MAIL_SMTP_HOST`, `MAIL_SMTP_PORT`,
`MAIL_SMTP_STARTTLS`; defaults suit Yahoo). Cloud Run must be able to reach the
SMTP port outbound, which it allows by default. Sending is untested against live
Yahoo, so send a first test message to yourself.

## Client access and verification

The configuration explicitly sets ingress to `all` and disables Cloud Run's
Invoker IAM check using Google's [recommended public-access option](https://docs.cloud.google.com/run/docs/authenticating/public).
This makes the HTTPS endpoint reachable by external MCP clients. **The MCP
endpoint still requires FastMCP's `Authorization: Bearer <MCP_AUTH_TOKEN>`.**
Only `/health` is intentionally unauthenticated. This does not implement OAuth
onboarding for ChatGPT connectors; that remains a separate milestone.

Organization policy may forbid disabling the Invoker IAM check. In that case,
keep Cloud Run IAM enabled and use a client/gateway that supplies a Google ID
token separately from the MCP bearer token; for example, Cloud Run accepts its
token in `X-Serverless-Authorization`. Configure the deployment flag accordingly.

After deployment, find the service URL in Cloud Run. Check `https://SERVICE_URL/health`
and connect an MCP client to `https://SERVICE_URL/mcp/` with your token. Confirm
that unauthenticated MCP calls fail. First test writes with disposable messages.
These configurations have not yet been submitted to GCP or tested on Cloud Run.
