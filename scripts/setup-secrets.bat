@echo off
REM Reads the gbrain-role connection string from the GBRAIN_RUNTIME_URL env var so
REM credentials are never committed. Example (cmd.exe):
REM   set GBRAIN_RUNTIME_URL=postgres://gbrain:PASSWORD@HOST:5432/gbrain?sslmode=require
REM   scripts\setup-secrets.bat
REM   set GBRAIN_RUNTIME_URL=
if "%GBRAIN_RUNTIME_URL%"=="" (
  echo Error: GBRAIN_RUNTIME_URL env var is required.
  echo Set it to the gbrain-role connection string before running this script.
  exit /b 1
)
gcloud services enable secretmanager.googleapis.com --project=dowd-assistant --quiet
gcloud secrets create GBRAIN_DATABASE_URL --replication-policy=automatic --project=dowd-assistant --quiet
echo %GBRAIN_RUNTIME_URL% | gcloud secrets versions add GBRAIN_DATABASE_URL --data-file=- --project=dowd-assistant --quiet
gcloud secrets add-iam-policy-binding GBRAIN_DATABASE_URL --member="serviceAccount:590600029741-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor" --project=dowd-assistant --quiet
gcloud run services update gbrain-server --region=us-central1 --project=dowd-assistant --remove-env-vars=DATABASE_URL --set-secrets=DATABASE_URL=GBRAIN_DATABASE_URL:latest --quiet
