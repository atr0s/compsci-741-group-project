#!/usr/bin/env bash
# Zava DIY - deploy the whole solution into one Azure resource group, phase by phase.
#
#   ./deploy.sh <env> <command> [args]
#
# Phases, in the order `all` runs them:
#   init     create .state/<env>.env: name suffix and generated secrets (idempotent)
#   infra    Bicep: every Azure resource, app setting and role assignment
#   db       the zava database, extensions and the store_manager role (SQL)
#   data     restore the retail backup, re-grant store_manager    [pg_restore 17+]
#   code     package src/ and zip-deploy the MCP server and the chat app
#   agents   publish the Foundry agents, then a smoke turn through the MCP server
#   verify   health and auth checks, then one chat turn through the deployed app
#
# Other commands:
#   whatif         preview what `infra` would change, without changing it
#   evals [ARGS]   run evals/run_evals.py against this environment (e.g. --only adv)
#   set KEY=VALUE  change a setting in the environment file (e.g. APP_SERVICE_SKU=F1)
#   show           print the environment's endpoints (no secrets)
#   down           delete the resource group and purge the Foundry account (asks first)
#
# Each phase reads what earlier phases wrote into .state/<env>.env, so any phase
# can be re-run on its own. See README.md.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/scripts/lib.sh"

usage() { sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-1}"; }

[ $# -ge 2 ] || usage
ENV_NAME=$1; COMMAND=$2; shift 2
[[ "$ENV_NAME" =~ ^[a-z][a-z0-9-]{2,13}$ ]] \
  || die "environment name must be 3-14 lowercase letters, digits or hyphens, starting with a letter"
mkdir -p "$STATE_DIR" "$BUILD_DIR"

# --------------------------------------------------------------------------- #

cmd_init() {
  ensure_login
  envfile default \
    "AZURE_ENV_NAME=$ENV_NAME" \
    "AZURE_SUBSCRIPTION_ID=$(az account show --query id -o tsv)" \
    "AZURE_LOCATION=${AZURE_LOCATION:-australiaeast}" \
    "AZURE_SUFFIX=$(openssl rand -hex 2)" \
    "APP_SERVICE_SKU=${APP_SERVICE_SKU:-B1}" \
    "BUDGET_ALERT_EMAILS=${BUDGET_ALERT_EMAILS:-}" \
    "MCP_API_KEY=$(gen_secret 32)" \
    "STORE_MANAGER_PASSWORD=$(gen_secret 24)" \
    "POSTGRES_ADMIN_PASSWORD=$(gen_secret 24)Aa1!" \
    "WEB_AUTH_USERS=${WEB_AUTH_USERS:-zava:$(gen_secret 12):all}"
  load_env
  ok "environment '$ENV_NAME' -> rg-$ENV_NAME-$AZURE_SUFFIX in $AZURE_LOCATION"
  ok "subscription $AZURE_SUBSCRIPTION_ID"
  echo "   state file: $(env_file)"
  [ -n "$BUDGET_ALERT_EMAILS" ] || warn "BUDGET_ALERT_EMAILS is empty, so no budget will be created (./deploy.sh $ENV_NAME set BUDGET_ALERT_EMAILS=you@example.com)"
}

write_params() {  # secret + per-environment parameters, never committed
  local ip=$1 out=$2
  python3 - "$out" "$ip" <<'PY'
import json, os, sys
out, ip = sys.argv[1], sys.argv[2]
e = os.environ
emails = [x.strip() for x in e.get("BUDGET_ALERT_EMAILS", "").split(",") if x.strip()]
params = {
    "environmentName": e["AZURE_ENV_NAME"],
    "location": e["AZURE_LOCATION"],
    "suffix": e["AZURE_SUFFIX"],
    "appServiceSku": e["APP_SERVICE_SKU"],
    "postgresAdminPassword": e["POSTGRES_ADMIN_PASSWORD"],
    "storeManagerPassword": e["STORE_MANAGER_PASSWORD"],
    "mcpApiKey": e["MCP_API_KEY"],
    "webAuthUsers": e["WEB_AUTH_USERS"],
    "clientIpAddresses": [ip] if ip else [],
    "budgetAlertEmails": emails,
}
doc = {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
       "contentVersion": "1.0.0.0",
       "parameters": {k: {"value": v} for k, v in params.items()}}
fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(doc, f)
PY
}

# Azure refuses to move a plan to F1 while any app on it has Always On, and the
# Bicep updates the plan before the apps - so switch it off on the existing apps
# first. A fresh environment has no apps yet and skips this.
release_always_on() {
  [ "$APP_SERVICE_SKU" = F1 ] || return 0
  local app
  for app in ${WEB_APP_NAME:-} ${MCP_APP_NAME:-}; do
    az webapp show -g "$AZURE_RESOURCE_GROUP" -n "$app" -o none 2>/dev/null || continue
    log "$app: switching Always On off before the move to F1"
    az webapp config set -g "$AZURE_RESOURCE_GROUP" -n "$app" --always-on false -o none
  done
}

run_bicep() {  # $1 = create | what-if
  load_env; check_subscription
  local ip; ip="$(client_ip)"
  [ -n "$ip" ] || warn "could not detect this machine's public IP - the db/data phases will not reach Postgres (set CLIENT_IP=...)"
  # The parameters file holds secrets: removed on every exit path, including a
  # failed deployment (a RETURN trap does not fire when set -e exits the script).
  PARAMS_FILE="$STATE_DIR/$ENV_NAME.params.json"
  trap 'rm -f "$PARAMS_FILE"' EXIT
  local params="$PARAMS_FILE"
  write_params "$ip" "$params"

  local args=(--location "$AZURE_LOCATION" --template-file "$DEPLOY_DIR/infra/main.bicep"
              --parameters @"$DEPLOY_DIR/infra/main.parameters.json" --parameters @"$params")
  if [ "$1" = what-if ]; then
    az deployment sub what-if "${args[@]}"
    return
  fi

  release_always_on
  log "deploying infrastructure to rg-$ENV_NAME-$AZURE_SUFFIX (10-20 minutes on a fresh environment)"
  local out="$STATE_DIR/$ENV_NAME.deployment.json"
  az deployment sub create --name "zava-$ENV_NAME-$(date +%Y%m%d%H%M%S)" "${args[@]}" -o json > "$out" \
    || die "infrastructure deployment failed - see the error above"
  envfile outputs "$out"
  load_env
  ok "infrastructure deployed: $AZURE_RESOURCE_GROUP"
}

cmd_infra()  { run_bicep create; }
cmd_whatif() { run_bicep what-if; }

cmd_db() {
  load_env; check_subscription
  require_vars POSTGRES_SERVER_FQDN POSTGRES_ENTRA_ADMIN STORE_MANAGER_PASSWORD
  pg_login
  log "creating the zava database (as $PGUSER)"
  psql_run --dbname postgres -f "$DEPLOY_DIR/sql/01_create_database.sql"
  log "extensions, store_manager role, retail schema"
  psql_run --dbname zava -v store_manager_password="$STORE_MANAGER_PASSWORD" -f "$DEPLOY_DIR/sql/02_bootstrap.sql"
  ok "database ready for the data load"
}

cmd_data() {
  load_env; check_subscription
  require_vars POSTGRES_SERVER_FQDN POSTGRES_ENTRA_ADMIN
  [ -f "$BACKUP_FILE" ] || die "backup not found: $BACKUP_FILE"
  resolve_pg_restore
  pg_login

  psql_run --dbname zava -f "$DEPLOY_DIR/sql/10_pre_restore.sql"

  local restore_log="$STATE_DIR/$ENV_NAME.restore.log"
  log "restoring $(basename "$BACKUP_FILE") with $PG_RESTORE (a few minutes)"
  # pg_restore exits 1 for ignorable errors (it reports "errors ignored on
  # restore"), so its exit code is not the verdict - the verification below is.
  # Expect exactly one: --clean tries to drop the vector extension, which
  # pg_diskann (created by the db phase) depends on, so vector simply stays.
  run_pg_restore 2>"$restore_log" || warn "pg_restore reported $(grep -c 'error:' "$restore_log" || true) error(s) - see $restore_log; verifying"

  psql_run --dbname zava -f "$DEPLOY_DIR/sql/20_post_restore.sql"

  log "verifying the load"
  local facts; facts="$(psql -X -At --dbname zava -f "$DEPLOY_DIR/sql/30_verify.sql")"
  echo "$facts" | sed 's/^/   /; s/|/: /'
  fact() { echo "$facts" | awk -F'|' -v k="$1" '$1==k {print $2}'; }
  [ "$(fact products)" -gt 0 ] && [ "$(fact stores)" -gt 0 ] || die "the restore left retail empty - see $restore_log"
  [ "$(fact rls_tables)" -gt 0 ] || die "no table has row level security enabled after the restore"
  [ "$(fact store_manager_can_read)" = "true" ] || die "store_manager cannot read retail.products"
  ok "data loaded, row level security in place"
}

cmd_code() {
  load_env; check_subscription
  require_vars MCP_APP_NAME WEB_APP_NAME MCP_HEALTH_URL WEB_APP_URL
  log "packaging src/"
  python3 "$DEPLOY_DIR/scripts/package.py" "$BUILD_DIR"
  deploy_zip "$MCP_APP_NAME" "$BUILD_DIR/mcp.zip" "$MCP_HEALTH_URL"
  deploy_zip "$WEB_APP_NAME" "$BUILD_DIR/web.zip" "$WEB_APP_URL/health"
}

cmd_agents() {
  load_env; check_subscription
  require_vars PROJECT_ENDPOINT MCP_SERVER_URL MCP_API_KEY GPT_MODEL_DEPLOYMENT_NAME
  ensure_venv
  local provision=("$VENV_PY" "$REPO_ROOT/scripts/provision_agent.py")
  log "publishing agents to $PROJECT_ENDPOINT"
  # Role assignments from the infra phase can take a few minutes to reach the
  # Foundry data plane, so a first attempt straight after `infra` may be refused.
  local attempt
  for attempt in 1 2 3 4; do
    "${provision[@]}" all --env-file "$(env_file)" && break
    [ "$attempt" -lt 4 ] || die "agent provisioning failed"
    warn "provisioning failed (attempt $attempt) - waiting 60s for role propagation"
    sleep 60
  done
  log "smoke turn: the DIY agent must reach the MCP server and answer"
  "${provision[@]}" diy --smoke --env-file "$(env_file)" || die "smoke turn failed"
  ok "agents published and answering"
}

cmd_verify() {
  load_env; check_subscription
  require_vars MCP_SERVER_URL MCP_HEALTH_URL WEB_APP_URL WEB_AUTH_USERS
  local failures=0
  check() {  # name expected actual
    if [ "$2" = "$3" ]; then ok "$1"; else warn "$1: expected $2, got $3"; failures=$((failures + 1)); fi
  }
  local first="${WEB_AUTH_USERS%%,*}"
  local user="${first%%:*}" rest="${first#*:}"; local pass="${rest%%:*}"

  check "MCP /healthz answers"                 200 "$(http_code "$MCP_HEALTH_URL")"
  check "MCP refuses a call without x-api-key" 401 "$(http_code -X POST "$MCP_SERVER_URL")"
  check "chat /health answers"                 200 "$(http_code "$WEB_APP_URL/health")"
  check "chat refuses an anonymous visitor"    401 "$(http_code "$WEB_APP_URL/")"
  check "chat admits a configured sign-in"     200 "$(http_code -u "$user:$pass" "$WEB_APP_URL/")"

  log "one chat turn through the deployed app (chat app -> Foundry agent -> MCP -> Postgres)"
  local message; message="$(python3 -c 'import urllib.parse; print(urllib.parse.quote("Do you have Rubber Mulch Black in stock?"))')"
  local stream="$STATE_DIR/$ENV_NAME.verify-stream.txt"
  curl -sN --max-time 420 -u "$user:$pass" "$WEB_APP_URL/chat/stream?agent=diy&message=$message" > "$stream" || true
  local deltas; deltas="$(grep -c '"content"' "$stream" || true)"
  if grep -q '"error"' "$stream"; then
    warn "chat turn returned an error: $(grep '"error"' "$stream" | head -1)"; failures=$((failures + 1))
  elif grep -q '\[DONE\]' "$stream" && [ "$deltas" -gt 0 ]; then
    ok "chat turn streamed $deltas content frames and completed"
    python3 - "$stream" <<'PY'
import json, sys
text = "".join(json.loads(l[6:]).get("content", "") for l in open(sys.argv[1]) if l.startswith("data: {"))
print("   " + text[:400].replace("\n", "\n   "))
PY
  else
    warn "chat turn did not complete - see $stream"; failures=$((failures + 1))
  fi

  [ "$failures" -eq 0 ] || die "$failures verification check(s) failed"
  ok "end-to-end verification passed"
}

cmd_evals() {
  load_env; check_subscription
  require_vars AGENT_NAME_DIY PROJECT_ENDPOINT MCP_API_KEY
  ensure_venv
  # The evals read the repo's root .env too, but only for keys not already set;
  # every key this environment defines has been exported above and wins.
  (cd "$REPO_ROOT" && "$VENV_PY" evals/run_evals.py "$@")
}

cmd_set() {
  [ $# -ge 1 ] || die "usage: ./deploy.sh $ENV_NAME set KEY=VALUE ..."
  load_env
  envfile set "$@"
  ok "updated $(env_file) - re-run the phase that uses it (e.g. infra)"
}

cmd_show() {
  load_env
  local k
  for k in AZURE_SUBSCRIPTION_ID AZURE_RESOURCE_GROUP AZURE_LOCATION APP_SERVICE_SKU PROJECT_ENDPOINT \
           GPT_MODEL_DEPLOYMENT_NAME POSTGRES_SERVER_FQDN MCP_SERVER_URL WEB_APP_URL \
           AGENT_NAME_DIY; do
    printf '  %-28s %s\n' "$k" "${!k:-<not yet deployed>}"
  done
  echo "  sign-ins                     (WEB_AUTH_USERS in $(env_file))"
}

cmd_down() {
  load_env; check_subscription
  require_vars AZURE_RESOURCE_GROUP FOUNDRY_ACCOUNT_NAME
  warn "this deletes $AZURE_RESOURCE_GROUP and everything in it, then purges $FOUNDRY_ACCOUNT_NAME"
  local answer; read -r -p "type the environment name to confirm: " answer
  [ "$answer" = "$ENV_NAME" ] || die "not confirmed"
  log "deleting $AZURE_RESOURCE_GROUP (several minutes)"
  az group delete -n "$AZURE_RESOURCE_GROUP" --yes
  # Foundry accounts are soft-deleted and keep their name reserved, so without a
  # purge a re-deploy with the same suffix fails.
  log "purging the soft-deleted Foundry account"
  az cognitiveservices account purge -l "$AZURE_LOCATION" -g "$AZURE_RESOURCE_GROUP" -n "$FOUNDRY_ACCOUNT_NAME" \
    || warn "purge failed - the name stays reserved until the retention period ends"
  ok "environment '$ENV_NAME' removed (state file kept: $(env_file))"
}

cmd_all() {
  cmd_init; cmd_infra; cmd_db; cmd_data; cmd_code; cmd_agents; cmd_verify
}

case "$COMMAND" in
  init|infra|whatif|db|data|code|agents|verify|evals|set|show|down|all) "cmd_$COMMAND" "$@" ;;
  -h|--help|help) usage 0 ;;
  *) usage ;;
esac
