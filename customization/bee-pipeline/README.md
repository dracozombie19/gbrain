# Bee → Brain Pipeline

GCP Cloud Function that pulls Bee wearable transcripts every 4 hours, extracts durable biographical facts via Claude on Vertex AI, and writes them as timeline entries on Brain person/pet pages via the Brain HTTP MCP server.

## Architecture

```
Cloud Scheduler (every 4h)
  → Cloud Function (Python 3.12 + Node.js + Bee CLI)
      ├── Bee CLI (`bee changed --json`)  — fetch new conversations via cursor
      ├── Vertex AI                       — Claude fact extraction (anthropic[vertex])
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

3. **Vertex AI enabled** in your GCP project with Anthropic Claude model access:
   - Enable Vertex AI API
   - Request access to Anthropic models on Vertex AI (Model Garden)
   - Cloud Function service account needs `roles/aiplatform.user`

4. **GCS bucket** for state storage (any existing bucket; pipeline writes to `bee-pipeline/` prefix).

5. **Secret Manager secrets** created:
   ```bash
   echo -n "YOUR_TOKEN"         | gcloud secrets create BEE_API_TOKEN --data-file=-
   echo -n "YOUR_BRAIN_URL"     | gcloud secrets create BRAIN_URL --data-file=-
   echo -n "YOUR_CLIENT_ID"     | gcloud secrets create BRAIN_CLIENT_ID --data-file=-
   echo -n "YOUR_SECRET"        | gcloud secrets create BRAIN_CLIENT_SECRET --data-file=-
   ```
   No `ANTHROPIC_API_KEY` needed — auth uses Application Default Credentials.

6. **Service account permissions**:
   - `roles/aiplatform.user` — Vertex AI
   - `roles/secretmanager.secretAccessor` — Secret Manager
   - `roles/storage.objectAdmin` — GCS bucket

## Alias Setup

Run this once after Brain is running to add aliases to family pages:
```bash
BRAIN_URL=http://localhost:9090 \
BRAIN_CLIENT_ID=... BRAIN_CLIENT_SECRET=... \
python update_aliases.py
```

The resolver uses these aliases to map spoken names (e.g. "Ashley", "Mama") to Brain page slugs.

## Local Testing

### Test Brain client
```bash
cd customization/bee-pipeline
pip install -r requirements.txt

BRAIN_URL=http://localhost:9090 \
BRAIN_CLIENT_ID=... BRAIN_CLIENT_SECRET=... \
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
# Fill in .env with real values (BEE_TOKEN, BRAIN_*, GCS_BUCKET, GCP_PROJECT)

source .env
functions-framework --target run_pipeline --debug
# Then POST to http://localhost:8080
```

## Deployment

The Cloud Function container needs Python + Node.js. Use a custom Dockerfile or Cloud Build steps.

### Dockerfile approach
```dockerfile
FROM python:3.12-slim

# Install Node.js for Bee CLI
RUN apt-get update && apt-get install -y nodejs npm && rm -rf /var/lib/apt/lists/*
RUN npm install -g @beeai/cli

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
CMD ["functions-framework", "--target=run_pipeline", "--port=8080"]
```

### Deploy to Cloud Run (recommended)
```bash
gcloud builds submit customization/bee-pipeline \
  --tag gcr.io/YOUR_PROJECT/bee-brain-pipeline

gcloud run deploy bee-brain-pipeline \
  --image gcr.io/YOUR_PROJECT/bee-brain-pipeline \
  --region YOUR_REGION \
  --no-allow-unauthenticated \
  --service-account YOUR_SA@YOUR_PROJECT.iam.gserviceaccount.com \
  --set-env-vars GCP_PROJECT=YOUR_PROJECT,GCS_BUCKET=YOUR_BUCKET \
  --memory=512Mi \
  --timeout=300s
```

### Cloud Scheduler trigger
```bash
gcloud scheduler jobs create http bee-brain-pipeline-trigger \
  --location=YOUR_REGION \
  --schedule="0 */4 * * *" \
  --uri="https://bee-brain-pipeline-HASH-YOUR_REGION.run.app" \
  --oidc-service-account-email=YOUR_SA@YOUR_PROJECT.iam.gserviceaccount.com
```

## How It Works

1. **State**: GCS stores `bee-pipeline/state.json` with the Bee cursor from the last run. On first run, cursor is null — fetches all available conversations.

2. **Fetch**: `bee changed --cursor CURSOR --json` returns all conversations newer than the cursor, plus a `meta.next_cursor` for the next run.

3. **Filter**: Only `state == "COMPLETED"` conversations with non-empty utterance text are processed.

4. **Extract**: Claude (via Vertex AI) reads each transcript and extracts durable biographical facts with attribution confidence and subject name.

5. **Route**:
   - High confidence + unambiguous + resolved person → `add_timeline_entry` on their Brain page
   - Everything else → `put_page` in `pending-review/` with tag `pending-review`

6. **Cursor advance**: After all conversations in the batch process, the new cursor is saved to GCS.

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
