@echo off
gcloud services enable secretmanager.googleapis.com --project=dowd-assistant --quiet
gcloud secrets create GBRAIN_DATABASE_URL --replication-policy=automatic --project=dowd-assistant --quiet
echo postgres://gbrain:y86kKGJX8Tda27FRZWeuAtR+@34.57.122.49:5432/gbrain | gcloud secrets versions add GBRAIN_DATABASE_URL --data-file=- --project=dowd-assistant --quiet
gcloud secrets add-iam-policy-binding GBRAIN_DATABASE_URL --member="serviceAccount:590600029741-compute@developer.gserviceaccount.com" --role="roles/secretmanager.secretAccessor" --project=dowd-assistant --quiet
gcloud run services update gbrain-server --region=us-central1 --project=dowd-assistant --remove-env-vars=DATABASE_URL --set-secrets=DATABASE_URL=GBRAIN_DATABASE_URL:latest --quiet
