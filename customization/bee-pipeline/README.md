# Bee → Brain Pipeline

GCP Cloud Run service that pulls Bee wearable transcripts every 4 hours, extracts durable biographical facts via Claude, and writes them as timeline entries on Brain person/pet pages via the Brain HTTP MCP server.

## Architecture

```
Cloud Scheduler (every 4h)
  → Cloud Run (Python 3.12 + Node.js + Bee CLI)
      ├── Bee CLI (`bee changed --json`)  — fetch new conversations via cursor
      ├── Anthropic API                   — Claude fact extraction (direct API key)
      ├── Brain HTTP MCP                  — write timeline entries + pending-review pages
      └── GCS                             — cursor state + failed extraction log
```

The pipeline uses the Bee CLI (`@beeai/cli`) as a subprocess rather than direct HTTP calls — the Bee API uses a private CA certificate, and the CLI handles TLS internally. The container must include Node.js.

## Getting the Bee Token

The Bee CLI authenticates via a long-lived JWT stored in Windows Credential Manager. Extract it once and store it in Secret Manager.

**On Windows (PowerShell):**
```powershell
Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public class CredMan {
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern bool CredRead(string target, uint type, int flags, out IntPtr cred);
    [DllImport("advapi32.dll")]
    public static extern void CredFree(IntPtr cred);
}
"@
$ptr = [IntPtr]::Zero
[CredMan]::CredRead("bee-cli/token:prod", 1, 0, [ref]$ptr) | Out-Null
$cred = [System.Runtime.InteropServices.Marshal]::PtrToStructure($ptr, [Type][System.Runtime.InteropServices.CREDENTIAL])
# Read raw bytes as UTF-8 (not UTF-16)
$bytes = New-Object byte[] $cred.CredentialBlobSize
[System.Runtime.InteropServices.Marshal]::Copy($cred.CredentialBlob, $bytes, 0, $cred.CredentialBlobSize)
[CredMan]::CredFree($ptr)
$token = [System.Text.Encoding]::UTF8.GetString($bytes)
Write-Host $token
```

Verify it works:
```bash
echo $token | bee login --token-stdin
bee status
```

## Prerequisites

1. **Brain running with `--http`** on the `personal-deploy` branch:
   ```bash
   gbrain serve --http --port 9090
   ```

2. **OAuth client registered** for the pipeline:
   ```bash
   gbrain auth register-client bee-pipeline --scopes read write
   ```
   Note the `client_id` and `client_secret` — you'll need them.

3. **Anthropic API key** — the pipeline calls the Anthropic API directly for fact extraction. No Vertex AI setup needed.

4. **GCS bucket** for state storage (any existing bucket; pipeline writes to `bee-pipeline/` prefix).

5. **Secret Manager secrets** created:
   ```bash
   echo -n "YOUR_TOKEN"         | gcloud secrets create BEE_API_TOKEN --data-file=-
   echo -n "YOUR_BRAIN_URL"     | gcloud secrets create BRAIN_URL --data-file=-
   echo -n "YOUR_CLIENT_ID"     | gcloud secrets create BRAIN_CLIENT_ID --data-file=-
   echo -n "YOUR_SECRET"        | gcloud secrets create BRAIN_CLIENT_SECRET --data-file=-
   echo -n "sk-ant-..."         | gcloud secrets create ANTHROPIC_API_KEY --data-file=-
   ```

6. **Service account permissions**:
   - `roles/secretmanager.secretAccessor` — Secret Manager
   - `roles/storage.objectAdmin` — GCS bucket

## Alias Setup

Run this once after Brain is running to add aliases to family pages:
```bash
GBRAIN_URL=http://localhost:9090 \
BEE_GBRAIN_CLIENT_ID=... BEE_GBRAIN_CLIENT_SECRET=... \
python update_aliases.py
```

The resolver uses these aliases to map spoken names (e.g. "Ashley", "Mama") to Brain page slugs.

## Local Testing

### Test Brain client
```bash
cd customization/bee-pipeline
pip install -r requirements.txt

GBRAIN_URL=http://localhost:9090 \
BEE_GBRAIN_CLIENT_ID=... BEE_GBRAIN_CLIENT_SECRET=... \
python test_brain_client.py
```

### Test Bee CLI
```bash
# Authenticate
echo $YOUR_TOKEN | bee login --token-stdin

# Fetch conversations
bee changed --json | python -m json.tool | head -50
```

### Run pipeline locally
```bash
cp .env.example .env
# Fill in .env with real values (BEE_API_TOKEN, GBRAIN_URL, BEE_GBRAIN_CLIENT_*, GCS_BUCKET)

source .env && python main.py
```

For a dry run (no Brain writes, local cursor state):
```bash
source .env && python dry_run.py
```

## Deployment

The container needs Python + Node.js (for the Bee CLI). See the `Dockerfile` in this directory.

### One-time setup

Create the Artifact Registry repository and configure Docker auth:
```bash
gcloud artifacts repositories create bee-brain-pipeline \
  --repository-format=docker \
  --location=us-central1 \
  --project=dowd-assistant

gcloud auth configure-docker us-central1-docker.pkg.dev
```

Grant the service account permission to execute the job (needed for Cloud Scheduler):
```bash
gcloud projects add-iam-policy-binding dowd-assistant \
  --member="serviceAccount:590600029741-compute@developer.gserviceaccount.com" \
  --role="roles/run.invoker"
```

### Build and create the job

```powershell
$tag = git rev-parse --short HEAD
$image = "us-central1-docker.pkg.dev/dowd-assistant/bee-brain-pipeline/pipeline:$tag"

# Build + push
gcloud builds submit --tag $image --project=dowd-assistant customization/bee-pipeline

# Create the job (first deploy)
gcloud run jobs create bee-brain-pipeline `
  --image $image `
  --region us-central1 `
  --service-account 590600029741-compute@developer.gserviceaccount.com `
  --set-env-vars "GCP_PROJECT=dowd-assistant,GCS_BUCKET=gbrain-storage-dowd-assistant,PENDING_REVIEW_ONLY=1" `
  --set-secrets "BEE_API_TOKEN=BEE_API_TOKEN:latest,GBRAIN_URL=GBRAIN_URL:latest,BEE_GBRAIN_CLIENT_ID=BEE_GBRAIN_CLIENT_ID:latest,BEE_GBRAIN_CLIENT_SECRET=BEE_GBRAIN_CLIENT_SECRET:latest,ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest" `
  --memory=512Mi `
  --task-timeout=1800s
```

On subsequent deploys, use `update` instead of `create`:
```powershell
gcloud run jobs update bee-brain-pipeline --image $image --region us-central1
```

To run manually at any time:
```bash
gcloud run jobs execute bee-brain-pipeline --region us-central1
```

### Cloud Scheduler trigger

```bash
gcloud scheduler jobs create http bee-brain-pipeline-schedule \
  --location=us-central1 \
  --schedule="0 */4 * * *" \
  --uri="https://us-central1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/dowd-assistant/jobs/bee-brain-pipeline:run" \
  --message-body="" \
  --oauth-service-account-email=590600029741-compute@developer.gserviceaccount.com
```

## How It Works

1. **State**: GCS stores `bee-pipeline/state.json` with the Bee cursor from the last run. On first run, cursor is null — fetches all available conversations.

2. **Fetch**: `bee changed --cursor CURSOR --json` returns all conversations newer than the cursor, plus a `meta.next_cursor` for the next run.

3. **Filter**: Only `state == "COMPLETED"` conversations with non-empty utterance text are processed.

4. **Extract**: Claude (via Anthropic API) reads each transcript and extracts durable biographical facts with attribution confidence and subject name.

5. **Route**:
   - High confidence + unambiguous + resolved person → `add_timeline_entry` on their Brain page
   - Everything else → `put_page` in `pending-review/` with tag `pending-review`

6. **Cursor advance**: After all conversations in the batch process, the new cursor is saved to GCS.

## Audit Mode (Pending-Review Only)

Set `PENDING_REVIEW_ONLY=1` to route **all** extractions to pending-review, including high-confidence ones. Useful when you want to audit the pipeline output before trusting it to write directly to Brain pages.

```bash
# Cloud Run: add to --set-env-vars
PENDING_REVIEW_ONLY=1

# Local dev
PENDING_REVIEW_ONLY=1 python -m main
```

High-confidence facts routed via audit mode are annotated with `**Note**: audit mode — routed to pending-review regardless of confidence.` in the pending-review page so you can distinguish them from genuinely ambiguous ones during triage.

When you're satisfied with the quality, remove `PENDING_REVIEW_ONLY` (or set it to `0`) and redeploy.

## Pending-Review Triage

Low-confidence or ambiguous extractions land in `pending-review/{date}-{id}-{n}` pages tagged `pending-review`. Use the `bee-triage` Brain skill to work through them:

```
/bee-triage
```

## State File

GCS: `{GCS_BUCKET}/bee-pipeline/state.json`
```json
{
  "cursor": "v1-1779159682636",
  "last_processed_at": "2026-05-18T12:00:00+00:00"
}
```

Failed extractions: `{GCS_BUCKET}/bee-pipeline/failed/{date}.jsonl`
