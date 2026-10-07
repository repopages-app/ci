#!/bin/sh
# Any CI: a connection test, then the client. REPOPAGES_SECRET comes from the CI's secret store
# (the secret shown once on the RepoPages settings page); never write it into the repository.
# Needs python3 and git. Diagrams need Chrome (Mermaid) and Java (PlantUML) on the runner, or use
# the Docker image ghcr.io/repopages-app/ci, which has both.
set -eu
: "${REPOPAGES_URL:?sync URL from Confluence settings > RepoPages}"
: "${REPOPAGES_SECRET:?}"
REPO=${REPO:-owner/name}      # exactly as mapped in RepoPages
PREFIX=${PREFIX:-docs/}       # the mapping's folder, "" for the whole repository

# 1. Connection test: an empty signed push. Afterwards the settings page shows
#    "Last request: accepted - ok" (a wrong secret shows "invalid signature").
body='{"repo":"'"$REPO"'","ref":"refs/heads/main","sha":"0000000000000000000000000000000000000000","shortSha":"0000000","pushedAt":"1970-01-01T00:00:00Z","files":[]}'
sig=$(printf '%s' "$body" | openssl dgst -sha256 -hmac "$REPOPAGES_SECRET" | sed 's/^.*= //')
curl -sS -X POST -H 'Content-Type: application/json' -H "X-RepoPages-Signature: sha256=$sig" --data-binary "$body" "$REPOPAGES_URL"
echo

# 2. On every push to the default branch, from the repository checkout:
python3 repopages_push.py push --repo "$REPO" --prefix "$PREFIX"
# First time, or to resend everything:  python3 repopages_push.py import --repo "$REPO" --prefix "$PREFIX"
