# Shared helpers for deploy.sh. Sourced, never executed.
# shellcheck shell=bash

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$DEPLOY_DIR/.." && pwd)"
STATE_DIR="$DEPLOY_DIR/.state"
BUILD_DIR="$DEPLOY_DIR/.build"
VENV_DIR="$DEPLOY_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
BACKUP_FILE="${BACKUP_FILE:-$REPO_ROOT/data/zava_retail_2025_07_21_postgres_rls.backup}"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERR\033[0m %s\n' "$*" >&2; exit 1; }

env_file() { echo "$STATE_DIR/$ENV_NAME.env"; }
envfile()  { python3 "$DEPLOY_DIR/scripts/envfile.py" "$(env_file)" "$@"; }

load_env() {
  [ -f "$(env_file)" ] || die "no environment '$ENV_NAME' - run: ./deploy.sh $ENV_NAME init"
  set -a
  # shellcheck disable=SC1090
  source "$(env_file)"
  set +a
}

require_vars() {
  local missing=()
  for v in "$@"; do [ -n "${!v:-}" ] || missing+=("$v"); done
  [ ${#missing[@]} -eq 0 ] || die "missing ${missing[*]} - has an earlier phase run? (./deploy.sh $ENV_NAME infra)"
}

gen_secret() { python3 -c 'import secrets,sys; print(secrets.token_urlsafe(int(sys.argv[1])))' "$1"; }

ensure_login() { az account show -o none 2>/dev/null || die "not logged in to Azure - run: az login"; }

# Every phase after init refuses to run against a subscription other than the one
# the environment was created in. Side-by-side deployments make the wrong
# `az account set` an easy mistake, and this turns it into an error.
check_subscription() {
  ensure_login
  local current; current="$(az account show --query id -o tsv)"
  [ "$current" = "$AZURE_SUBSCRIPTION_ID" ] \
    || die "signed in to subscription $current, but '$ENV_NAME' lives in $AZURE_SUBSCRIPTION_ID (az account set -s $AZURE_SUBSCRIPTION_ID)"
}

client_ip() {
  if [ -n "${CLIENT_IP:-}" ]; then echo "$CLIENT_IP"; return; fi
  curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true
}

http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$@" || true; }

# Poll until a URL answers with the expected status. Cold starts on App Service
# take ~2 minutes, so a single request is not evidence of anything.
wait_http() {
  local url=$1 expect=$2 timeout=${3:-600} code=""
  local deadline=$((SECONDS + timeout))
  while [ $SECONDS -lt $deadline ]; do
    code="$(http_code "$url")"
    [ "$code" = "$expect" ] && return 0
    sleep 10
  done
  die "$url never returned $expect within ${timeout}s (last: ${code:-none})"
}

ensure_venv() {
  if [ ! -x "$VENV_PY" ] || ! "$VENV_PY" -c 'import azure.ai.projects, openai, dotenv' 2>/dev/null; then
    log "creating $VENV_DIR for the data-plane scripts"
    python3 -m venv "$VENV_DIR"
    "$VENV_PY" -m pip install -q --upgrade pip
    "$VENV_PY" -m pip install -q -r "$DEPLOY_DIR/requirements.txt"
  fi
}

# --- Postgres -------------------------------------------------------------- #

# Connects as the deployer, who Bicep made the server's Entra admin, with a
# short-lived token as the password. Nothing here uses the password admin.
pg_login() {
  export PGHOST="$POSTGRES_SERVER_FQDN" PGUSER="$POSTGRES_ENTRA_ADMIN" PGPORT=5432 PGSSLMODE=require
  PGPASSWORD="$(az account get-access-token --resource https://ossrdbms-aad.database.windows.net --query accessToken -o tsv)"
  export PGPASSWORD
}

psql_run() { psql -X -v ON_ERROR_STOP=1 -q "$@"; }

# The backup was written by PostgreSQL 17 and older pg_restore cannot read its
# header. Look for a 17+ client, else fall back to the postgres:17 image.
resolve_pg_restore() {
  local dir cand major
  for dir in "${PG_BIN:-}" /opt/homebrew/opt/postgresql@17/bin /usr/local/opt/postgresql@17/bin \
             /usr/lib/postgresql/17/bin ""; do
    cand="${dir:+$dir/}pg_restore"
    command -v "$cand" >/dev/null 2>&1 || continue
    major="$("$cand" --version | grep -oE '[0-9]+' | head -1)"
    if [ "${major:-0}" -ge 17 ]; then PG_RESTORE="$cand"; return 0; fi
  done
  if docker info >/dev/null 2>&1; then PG_RESTORE=docker; return 0; fi
  die "pg_restore 17+ is required to read the backup, and none was found.
     Either: brew install postgresql@17   (keg-only, does not replace an older psql)
     or:     start Docker, and postgres:17 will be used
     or:     PG_BIN=/path/to/pg17/bin ./deploy.sh $ENV_NAME data"
}

run_pg_restore() {
  local args=(--dbname zava --clean --if-exists --no-owner --no-privileges)
  if [ "$PG_RESTORE" = docker ]; then
    docker run --rm -e PGHOST -e PGUSER -e PGPASSWORD -e PGPORT -e PGSSLMODE \
      -v "$(dirname "$BACKUP_FILE"):/backup:ro" postgres:17 \
      pg_restore "${args[@]}" "/backup/$(basename "$BACKUP_FILE")"
  else
    "$PG_RESTORE" "${args[@]}" "$BACKUP_FILE"
  fi
}

# --- App Service ----------------------------------------------------------- #

# Kudu takes an Entra token, not publishing credentials: basic auth is disabled
# on these sites.
kudu_get() {
  local token; token="$(az account get-access-token --resource https://management.core.windows.net/ --query accessToken -o tsv)"
  curl -fsS --max-time 30 -H "Authorization: Bearer $token" "https://$1.scm.azurewebsites.net$2"
}

latest_deployment() {  # prints "id status complete"
  kudu_get "$1" /api/deployments/latest 2>/dev/null \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("id",""), d.get("status",""), d.get("complete",""))' \
      2>/dev/null || true
}

# Deploy with the site STOPPED. Hot zip deploys to these sites have died with
# "Deployment has been stopped due to SCM container restart"; the same package
# deploys cleanly while the site is stopped. And judge by Kudu's record and by
# the health endpoint, not by the CLI: `az webapp deploy` times out while Oryx is
# still building and reports failure for deploys that landed.
deploy_zip() {
  local app=$1 zip=$2 health=$3 before id status complete
  before="$(latest_deployment "$app" | cut -d' ' -f1)"

  log "$app: stopping for the deploy"
  az webapp stop -g "$AZURE_RESOURCE_GROUP" -n "$app" -o none

  log "$app: uploading $(basename "$zip")"
  # --track-status false: the CLI otherwise waits for the site to start, which a
  # stopped site never does, and fails after ~11 minutes. Kudu is polled below.
  az webapp deploy -g "$AZURE_RESOURCE_GROUP" -n "$app" --src-path "$zip" --type zip --async true \
     --track-status false -o none 2>"$STATE_DIR/$ENV_NAME.$app.deploy.err" \
    || warn "$app: the deploy call returned non-zero - polling Kudu for the real outcome"

  log "$app: waiting for the build"
  local deadline=$((SECONDS + 1500))
  while [ $SECONDS -lt $deadline ]; do
    read -r id status complete <<<"$(latest_deployment "$app")"
    if [ -n "${id:-}" ] && [ "$id" != "$before" ] && [ "${complete:-}" = "True" ]; then break; fi
    sleep 15
  done
  [ "${complete:-}" = "True" ] && [ "${id:-}" != "$before" ] || die "$app: no completed deployment after 25 minutes"
  # 4 = success, 3 = failed. A "failed" record has been seen alongside a build
  # that landed, so a failure is reported but the health check has the final say.
  [ "$status" = "4" ] || warn "$app: Kudu reports deployment status $status - checking health anyway"

  log "$app: starting"
  az webapp start -g "$AZURE_RESOURCE_GROUP" -n "$app" -o none
  log "$app: waiting for $health (cold start is ~2 minutes)"
  wait_http "$health" 200 900
  ok "$app is up"
}
