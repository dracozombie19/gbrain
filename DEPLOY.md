# Personal fork maintenance

This is your personal fork of [garrytan/gbrain](https://github.com/garrytan/gbrain).
It lives at [dracozombie19/gbrain](https://github.com/dracozombie19/gbrain) and
contains your deployment files (Dockerfile + setup scripts) on a long-lived
branch called `personal-deploy`.

## Remotes

```
origin    https://github.com/dracozombie19/gbrain.git   (your fork)
upstream  https://github.com/garrytan/gbrain.git        (the source)
```

If these ever drift, fix with:

```bash
git remote set-url origin   https://github.com/dracozombie19/gbrain.git
git remote set-url upstream https://github.com/garrytan/gbrain.git
```

## Branch layout

- **`master`** — clean mirror of `upstream/master`. Never commit directly to
  this branch. It exists only so you can fast-forward from upstream cleanly.
- **`personal-deploy`** — your working branch. Contains everything on `master`
  PLUS your `Dockerfile` and `scripts/` deployment helpers. Day-to-day edits
  go here.

## Pulling in new upstream releases

When the upstream repo ships a new version (e.g. v0.34, v1.0), bring it down
and refresh both your remote fork and your local CLI:

```powershell
# 1. Fast-forward your local master to upstream
git checkout master
git pull upstream master --ff-only

# 2. Mirror master to your fork on GitHub
git push origin master

# 3. Merge the new upstream changes into your deploy branch
git checkout personal-deploy
git merge master
git push origin personal-deploy

# 4. Refresh your local gbrain CLI (used for running migrations + doctor + etc)
bun install                              # pull any new deps
bun run build                            # produces bin/gbrain.exe
Copy-Item bin\gbrain.exe $env:USERPROFILE\.bun\bin\ -Force
gbrain --version                         # verify the new version is live
```

Conflicts during step 3 are rare because your changes are isolated (Dockerfile,
`DEPLOY.md`, and `scripts/setup-*`, `scripts/grant-*`, `scripts/gen-*` are
filenames upstream doesn't touch). If upstream ever adds its own Dockerfile,
you'll need to resolve that one file by hand.

If you prefer rebase over merge, use `git rebase master` instead of `git merge
master`, but be aware it rewrites `personal-deploy`'s history and forces a
`--force-with-lease` push afterward. For a personal fork, merge is simpler.

After step 4 your local `gbrain` matches what Cloud Run is about to deploy. The
next time you do a Cloud Run deploy, use this local CLI for the migration step
(see [Step 1 of Actual deployment](#step-1--apply-pending-migrations-as-a-bypassrls-role)).

## Day-to-day deploy edits

```bash
git checkout personal-deploy
# edit Dockerfile, scripts/, etc.
git add Dockerfile scripts/whatever.sh
git commit -m "Tweak deploy step X"
git push
```

That's it. No PR ceremony needed since this is your own fork.

## Cloning fresh on a new machine

```powershell
# Clone your fork and wire up upstream
git clone https://github.com/dracozombie19/gbrain.git
cd gbrain
git remote add upstream https://github.com/garrytan/gbrain.git
git fetch upstream
git checkout personal-deploy

# Install bun if you don't have it
powershell -c "irm bun.sh/install.ps1|iex"
# (close and re-open PowerShell so bun is on PATH)

# Install deps and build the local CLI
bun install
bun run build
Copy-Item bin\gbrain.exe $env:USERPROFILE\.bun\bin\ -Force
gbrain --version
```

## Local gbrain CLI

You'll want the `gbrain` binary on your PATH so you can run admin commands
(`gbrain apply-migrations`, `gbrain doctor`, `gbrain stats`) against the
production database from your workstation.

The build script in `package.json` runs `bun build --compile` to produce a
standalone Windows .exe under `bin/`. That .exe has no runtime dependencies on
`node_modules/` or bun itself, so dropping it into a directory on PATH is
enough:

```powershell
bun install                                              # only if deps changed
bun run build                                            # produces bin/gbrain.exe
Copy-Item bin\gbrain.exe $env:USERPROFILE\.bun\bin\ -Force
gbrain --version                                         # verify
```

Re-run those four lines after every upstream sync so your local CLI matches
what's deployed (it's step 4 of the upstream-release flow above).

To talk to the production database from this CLI:

```powershell
$env:DATABASE_URL = gcloud secrets versions access latest --secret=GBRAIN_DATABASE_URL --project=dowd-assistant
gbrain doctor --json | Select-Object -First 50          # quick health check
gbrain stats                                             # page/chunk counts
Remove-Item Env:DATABASE_URL
```

Always unset `DATABASE_URL` when you're done — a stray env var pointing your
local CLI at production is how accidents happen.

## Windows line-ending gotcha

Git on Windows auto-converts LF -> CRLF on checkout. For shell scripts and
Dockerfiles that get copied into Linux containers, this can cause `exec format
error` or shebang-line failures at runtime.

If you hit that, add a `.gitattributes` file at the repo root with:

```
Dockerfile      text eol=lf
*.sh            text eol=lf
scripts/*.sh    text eol=lf
```

Then re-checkout the affected files:

```bash
git rm --cached -r .
git reset --hard
```

Not urgent — only do this if you actually see breakage during a container build
or run. Your `.ps1` and `.bat` scripts run on Windows, so they should stay CRLF.

## Things to NEVER commit

- `secret_iam.json` (service account credentials)
- `gbrain_service.json` (if it contains creds)
- `.env`, `.env.local`, or anything with API keys
- Anything matching `*.pem`, `*.key`, `*-credentials.json`

If you need to track which secret files exist without committing them, add
them to `.gitignore` and document their purpose in this file instead.


## Actual deployment (GCP Cloud Run)

### One-time secret setup

Already done, kept here for disaster recovery / new project bootstrap:

```powershell
# Database connection string
echo -n "postgres://..." | gcloud secrets create GBRAIN_DATABASE_URL --data-file=- --project=dowd-assistant

# Cloud Storage HMAC access key + secret
echo -n "ACCESS_KEY_VALUE" | gcloud secrets create GBRAIN_STORAGE_ACCESS_KEY --data-file=- --project=dowd-assistant
echo -n "SECRET_KEY_VALUE" | gcloud secrets create GBRAIN_STORAGE_SECRET_KEY --data-file=- --project=dowd-assistant

# Grant the Cloud Run runtime service account read access to each secret
$SA = "590600029741-compute@developer.gserviceaccount.com"
foreach ($secret in @("GBRAIN_DATABASE_URL","GBRAIN_STORAGE_ACCESS_KEY","GBRAIN_STORAGE_SECRET_KEY")) {
  gcloud secrets add-iam-policy-binding $secret `
    --member="serviceAccount:$SA" `
    --role="roles/secretmanager.secretAccessor" `
    --project=dowd-assistant
}
```

To rotate a secret without redeploying, add a new version: `gcloud secrets versions add NAME --data-file=-`. The deploy command references `:latest` so the next Cloud Run revision picks it up automatically. Existing revisions keep using the version they were deployed with.

### Step 1 — apply pending migrations from your workstation

**Why migrations aren't in the container:** Cloud Run's startup probe is too
tight to run cold migrations on a populated brain — the container would
crash-loop on the first deploy after a big upstream schema bump. Running
migrations from your workstation also surfaces failures loudly in your
terminal instead of burying them in Cloud Run logs.

```powershell
$env:DATABASE_URL = gcloud secrets versions access latest --secret=GBRAIN_DATABASE_URL --project=dowd-assistant
gbrain apply-migrations --yes
Remove-Item Env:DATABASE_URL
```

The `gbrain` role has `BYPASSRLS` (granted once via the bootstrap below), so it
can apply RLS-related migrations using the existing `GBRAIN_DATABASE_URL`. No
separate admin connection is needed for routine deploys.

If a migration fails, **stop and fix before deploying** — the new code may
assume schema state that doesn't exist yet.

#### One-time bootstrap: grant BYPASSRLS to the gbrain role

Already done, kept here for disaster recovery. Without this, migration v24
(`rls_backfill_missing_tables`) fails because the `gbrain` role can't enable
RLS. Run once per database, as `postgres`:

```powershell
$env:POSTGRES_ADMIN_URL = "postgres://postgres:PASSWORD@HOST:5432/gbrain?sslmode=require"
bun run scripts/grant-privileges.ts
Remove-Item Env:POSTGRES_ADMIN_URL
```

The script also tries to `GRANT ALL` on existing tables. On Cloud SQL this
fails partway through because the managed `postgres` role isn't a true Postgres
superuser and can't grant on tables it doesn't own — that's expected and OK.
The `gbrain` role already owns the tables it created and has full access to
them; the only critical statement is the BYPASSRLS grant on the first line.

### Step 2 — build + deploy

From the repo root on the `personal-deploy` branch:

```powershell
# Tag the image with the current git SHA so each Cloud Run revision traces back to an exact commit
$tag = git rev-parse --short HEAD
$image = "us-central1-docker.pkg.dev/dowd-assistant/gbrain-repo/gbrain-server:$tag"

# Build + push to Artifact Registry
gcloud builds submit --tag $image --project=dowd-assistant .

# Deploy to Cloud Run
gcloud run deploy gbrain-server `
  --image $image `
  --region us-central1 `
  --project dowd-assistant `
  --memory 2Gi `
  --cpu 2 `
  --concurrency 80 `
  --timeout 300 `
  --min-instances 0 `
  --set-secrets="DATABASE_URL=GBRAIN_DATABASE_URL:latest,GBRAIN_STORAGE_ACCESS_KEY=GBRAIN_STORAGE_ACCESS_KEY:latest,GBRAIN_STORAGE_SECRET_KEY=GBRAIN_STORAGE_SECRET_KEY:latest" `
  --set-env-vars="GBRAIN_HTTP_TRUST_PROXY=1,GBRAIN_STORAGE_BACKEND=s3,GBRAIN_STORAGE_BUCKET=gbrain-storage-dowd-assistant,GBRAIN_STORAGE_REGION=us-central1,GBRAIN_STORAGE_ENDPOINT=https://storage.googleapis.com,GBRAIN_HTTP_PUBLIC_URL=https://gbrain-server-590600029741.us-central1.run.app"
```

### Why each flag is the way it is

- **`$tag = git rev-parse --short HEAD`** — image tag traces back to an exact commit. Beats manual `:v4` -> `:v5` versioning which drifts from what's actually in git. Open a revision in the Cloud Run console and you know exactly what code shipped.
- **`--memory 2Gi --cpu 2`** — gbrain runs PGLite WASM, embedding clients, and tree-sitter. The Cloud Run default of 512Mi / 1 CPU is too tight. Bump higher if you see memory pressure in the logs.
- **`--concurrency 80`** — Cloud Run default. Each container handles up to 80 simultaneous requests. For a personal MCP server this is plenty.
- **`--timeout 300`** — 5 minute request timeout. Some gbrain operations (embed, sync) can take a while.
- **`--min-instances 0`** — cold starts allowed (cheaper). First request after idle takes ~5-10s to warm up. If you want it always warm, switch to `--min-instances 1` (adds roughly $5-10/month for a 2GiB instance).
- **`--set-secrets`** — no plain-text credentials. Each secret is mounted as an env var at runtime; nothing is baked into the image or the revision config.
- **Container does NOT run migrations on startup** — Cloud Run's startup probe is too tight for cold migrations on a populated brain. Migrations happen in Step 1.

### After deploy: verify

```powershell
# Quick liveness probe
curl https://gbrain-server-590600029741.us-central1.run.app/health
```

Then check the new revision's startup log in the Cloud Run console. The container should reach the `gbrain serve` listening message within a few seconds — no migration output, since migrations were already applied in Step 1.

If something looks wrong, roll back to the previous revision with one click in the Cloud Run console under "Revisions" → "Manage traffic".

### Future improvements (not done yet)

- **Dedicated service account**: currently uses the default compute SA (`590600029741-compute@...`), which inherits broad project permissions. Create a `gbrain-runtime` SA with only Secret Manager Accessor + Storage Object Admin on the gbrain bucket, then deploy with `--service-account=gbrain-runtime@dowd-assistant.iam.gserviceaccount.com`. Defer until you have more than one workload sharing this project.
- **Cloud Build trigger**: a trigger on pushes to `origin/personal-deploy` could automate builds. For now, manual deploy from your laptop is fine.
