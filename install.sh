#!/usr/bin/env bash
# Deploy / redeploy the n8n stack and (re)install the workflow.
# Safe to re-run: the workflow JSON carries a stable id, so import updates it.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example — fill in the tokens, then re-run ./install.sh" >&2
  exit 1
fi

docker compose up -d

echo "Waiting for n8n to become healthy..."
for _ in $(seq 1 90); do
  if docker exec n8n-github-tracker node -e "fetch('http://127.0.0.1:5678/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" 2>/dev/null; then
    break
  fi
  sleep 2
done

docker exec -u node n8n-github-tracker n8n import:workflow \
  --input=/workflows/github-release-tracker.json

docker exec -u node n8n-github-tracker n8n publish:workflow --id=ghReleaseTrack01

# A running process only re-registers schedules it knew about at startup.
docker compose restart n8n >/dev/null
for _ in $(seq 1 90); do
  if docker exec n8n-github-tracker node -e "fetch('http://127.0.0.1:5678/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" 2>/dev/null; then break; fi
  sleep 2
done

echo
echo "Workflow installed and published."
EDITOR=$(grep -m1 '^N8N_EDITOR_BASE_URL=' .env | cut -d= -f2-)
echo "Editor: ${EDITOR:-http://localhost:5678}"
