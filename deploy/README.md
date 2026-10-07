# Deploying the Zava DIY solution

One command deploys the whole solution into a fresh resource group:

```bash
./deploy/deploy.sh <env> all
```

Each piece is also a phase you can run on its own:

| Phase | What it does | How |
|---|---|---|
| `init` | Creates `.state/<env>.env`: a 4-character name suffix and generated secrets. Idempotent, keeps existing values. | shell |
| `infra` | Every Azure resource: Foundry account, project and model deployments, Postgres, the App Service plan and both apps with their settings, managed identities and role assignments, monitoring, budget. | **Bicep** (`infra/main.bicep`) |
| `db` | The `zava` database, the `vector` and `pg_diskann` extensions, the `store_manager` role and the `retail` schema. | SQL (`sql/0*.sql`) |
| `data` | Restores `data/zava_retail_2025_07_21_postgres_rls.backup` and re-grants `store_manager`. **This is the data-loading step**; swap it for your own load. | `pg_restore` + SQL |
| `code` | Packages `src/` into two zips and deploys the MCP server and the chat app. | `az webapp deploy` |
| `agents` | Publishes the Foundry agents, then runs a smoke turn that has to reach the MCP server. | `scripts/provision_agent.py` |
| `verify` | Health and auth checks on both apps, then one real chat turn through the deployed chat app. | shell |

Every phase reads what earlier phases wrote to `.state/<env>.env`, so any of them can be re-run by itself. Re-running `infra` updates the environment in place.

## Why not all Bicep

Three things have no ARM representation, so they're scripted phases rather than template resources:

- **App code.** Bicep creates the sites; shipping code to them is a zip deploy.
- **What's inside Postgres.** Extensions, roles, grants and the data are SQL. The database itself is created in SQL too: if ARM creates it, the password admin owns it and the Entra admin that restores into it can't create the `retail` schema.
- **Foundry agents.** Agent versions (instructions, tools, structured inputs) are data-plane objects. The ARM type `accounts/projects/applications` only *publishes* an existing agent version; it can't define one.

## Prerequisites

- An Azure subscription, and `az login` as a **user** who can create resource groups and role assignments (Owner, or Contributor + User Access Administrator). That user becomes the Postgres Entra admin and gets data-plane access to Foundry.
- Regional quota in the target region (default `australiaeast`) for `gpt-5-mini` (250K TPM) and `text-embedding-3-small` (120K TPM) GlobalStandard, a Postgres Flexible Server B1ms, and an App Service B1 plan.
- `az` with Bicep, `python3`, `psql`, `jq`, `curl`, `openssl`.
- **For the `data` phase only:** `pg_restore` 17 or newer, because the backup's format is too new for older clients. Use either `brew install postgresql@17` (keg-only, so it doesn't replace an older `psql`) or a running Docker, in which case `postgres:17` is used. You can also point `PG_BIN` at a PostgreSQL 17 `bin/` directory.

The `agents` and `evals` commands create `deploy/.venv` from `requirements.txt` the first time they run.

## Configuration

Set these before `init` to override the defaults, or later with `./deploy/deploy.sh <env> set KEY=VALUE`:

| Key | Default | |
|---|---|---|
| `AZURE_LOCATION` | `australiaeast` | |
| `APP_SERVICE_SKU` | `B1` | `F1` is free but deploys on it are fragile. The usual pattern is to deploy on B1, then `set APP_SERVICE_SKU=F1` and re-run `infra` (Always On is switched off automatically). |
| `BUDGET_ALERT_EMAILS` | empty | Comma-separated. If empty, no budget is created. |
| `WEB_AUTH_USERS` | `zava:<random>:all` | Chat sign-ins, `name:password:store\|store`, comma-separated. |
| `CLIENT_IP` | auto-detected | Your public IP, which is opened on the Postgres firewall for the `db` and `data` phases. |

Secrets (`MCP_API_KEY`, `STORE_MANAGER_PASSWORD`, `POSTGRES_ADMIN_PASSWORD`) are generated once and kept in `.state/<env>.env`. That directory is git-ignored: **never commit it**.

## Other commands

```bash
./deploy/deploy.sh <env> whatif      # preview what infra would change
./deploy/deploy.sh <env> show        # endpoints, no secrets
./deploy/deploy.sh <env> evals --only adv   # the behavioural evals against this environment
./deploy/deploy.sh <env> down        # delete the resource group and purge the Foundry account
```

## Known limits

- The deploying identity is assumed to be a user. A service principal (CI) needs `deployerPrincipalType=ServicePrincipal` and has not been tested.
- Application Insights is created but neither app sends telemetry to it yet; that needs an OpenTelemetry SDK in the apps.
- Chat-session to Foundry-conversation mapping is in memory, so the chat app must stay on a single instance.
