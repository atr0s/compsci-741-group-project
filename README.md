# Zava DIY Assistant — COMPSCI 714 Group Project (Team BrainWave)

A proof-of-concept AI agent for **Zava**, a fictional hardware and home-improvement retailer with
7 stores plus an online store. The agent helps a DIY customer:

- **find products**, by describing what they need in plain language, with real prices and
  per-store stock, and
- **cost a project**: it scopes the job (store first, then the details), estimates quantities
  with waste allowances, and returns a list of real Zava products that has been verified
  against the catalogue.

It answers only from Zava's catalogue, refers regulated work (electrical, plumbing, structural)
to a licensed trade, and doesn't offer things it can't do, such as holds, orders or delivery.

This repository holds everything needed to deploy the solution from nothing into an Azure
subscription, and to test it.

## Architecture

The diagram source is [`docs/zava-diy.drawio`](docs/zava-diy.drawio); open it in
[draw.io](https://app.diagrams.net) or the VS Code draw.io extension.

| Tier | Component | Where |
|---|---|---|
| User | Browser chat UI from the upstream Zava workshop, adapted to show each tool call as a status line. Answers stream back via server-sent events | `src/shared/static/` |
| App | Chat app (FastAPI) from the upstream Zava workshop, adapted to run on **Azure App Service** against Foundry: HTTP Basic sign-in; sets the identity each turn runs as | `src/python/web_app/` |
| Agent | **Microsoft Foundry Agent Service**: agent `zava-diy-advisor` on `gpt-5-mini`. Foundry runs the tool loop and enforces the agent's tool allow-list | `scripts/provision_agent.py`, prompt in `src/python/workshop/prompts/diy_assistant.md` |
| Tools | **Catalog MCP server** (streamable HTTP) on App Service. 7 tools, each running fixed, parameterised SQL; every request needs an `x-api-key` | `src/python/mcp_server/catalog/` |
| Embeddings | Azure OpenAI `text-embedding-3-small` (1536 dims) for semantic search | same Foundry account |
| Data | **Azure Database for PostgreSQL Flexible Server 17**: `pgvector`, row-level security, least-privilege `store_manager` role | `deploy/sql/`, `data/` |
| Ops | Application Insights, Log Analytics, Azure budget alerts | `deploy/infra/modules/` |

**MCP tools**

| Tool | Purpose |
|---|---|
| `get_stores` | The stores and their ids |
| `get_taxonomy` | The 9 categories and 89 product types |
| `get_current_utc_date` | Today's date |
| `search_by_text` | Semantic search over product descriptions |
| `search_by_keyword` | Full-text search |
| `filter_candidates` | Price, category, type, store and stock filtering; also verifies the final list |
| `get_stock_by_store` | Stock at each store |

No tool reads orders or customers. Arguments, return columns, limits and the security model are
documented in [`src/python/mcp_server/catalog/README.md`](src/python/mcp_server/catalog/README.md).

**Security model:**
- The model never sees the MCP key or the row-level-security identity: Foundry writes them into
  the MCP request headers from structured inputs supplied for each turn.
- Postgres RLS decides which rows come back, and the tool list decides what data can be reached.
- **Known weakness:** the tool allow-list is enforced in Foundry, not in the MCP server, so anyone
  holding the shared key can call any of the 7 tools.

## Repository layout

```
deploy/        one-command deployment: deploy.sh, Bicep (infra/), SQL (sql/), helper scripts
scripts/       provision_agent.py - publishes the Foundry agent
src/python/
  mcp_server/  catalog MCP server (+ the embeddings client it imports from customer_sales/)
  web_app/     chat app (adapted from the upstream workshop)
  workshop/prompts/diy_assistant.md   the agent's instructions
src/shared/static/   chat UI (adapted from the upstream workshop)
evals/         behavioural evals (run_evals.py) and offline tests of the checks (test_checks.py)
data/          Zava retail dataset (PostgreSQL 17 backup, with RLS and precomputed embeddings)
docs/          architecture diagram
```

## Deploying

Full detail is in [`deploy/README.md`](deploy/README.md). In short:

### Prerequisites

- An Azure subscription, and `az login` as a **user** who can create resource groups and role
  assignments (Owner, or Contributor plus User Access Administrator).
- Regional quota (default `australiaeast`) for `gpt-5-mini` (250K TPM) and
  `text-embedding-3-small` (120K TPM) GlobalStandard, a Postgres Flexible Server B1ms, and an App
  Service B1 plan.
- Tools: `az` (with Bicep), `python3`, `psql`, `jq`, `curl`, `openssl`.
- For the data load: `pg_restore` **17 or newer** (`brew install postgresql@17`), or a running
  Docker, in which case the `postgres:17` image is used.

### One command

```bash
export BUDGET_ALERT_EMAILS=you@example.com   # optional; without it no budget is created
./deploy/deploy.sh <env> all                 # e.g. ./deploy/deploy.sh zava all
```

This runs the phases in order. Each one can also be re-run on its own:

| Phase | What it does |
|---|---|
| `init` | Creates `deploy/.state/<env>.env` with a name suffix and generated secrets |
| `infra` | Every Azure resource, via Bicep (`deploy/infra/main.bicep`) |
| `db` | Creates the `zava` database, extensions, the `store_manager` role and the `retail` schema |
| `data` | Restores `data/zava_retail_2025_07_21_postgres_rls.backup`. One `pg_restore` error, "cannot drop extension vector", is expected and harmless |
| `code` | Packages `src/` and zip-deploys the MCP server and the chat app |
| `agents` | Publishes `zava-diy-advisor` and runs a smoke turn that must reach the MCP server |
| `verify` | Health and auth checks on both apps, then one real chat turn end to end |

A fresh environment takes roughly 30–40 minutes.

`./deploy/deploy.sh <env> show` prints the chat URL and endpoints. The chat sign-in is
`WEB_AUTH_USERS` in `deploy/.state/<env>.env`. That file holds every generated secret, and
`.gitignore` excludes it: **never commit it**.

### Keeping costs down

- Run on B1 while working. When idle, drop to F1:
  `./deploy/deploy.sh <env> set APP_SERVICE_SKU=F1 && ./deploy/deploy.sh <env> infra`.
  This also switches Always On off.
- Stop Postgres when it isn't in use:
  `az postgres flexible-server stop -g <rg> -n <server>`.
- Tear everything down with `./deploy/deploy.sh <env> down`, which deletes the resource group and
  purges the Foundry account.

## Testing

**Offline tests of the eval checks.** No model and no spend; each check is exercised with a
transcript it must pass and one it must fail:

```bash
MCP_API_KEY=any-value python3 evals/test_checks.py   # exit code = number of checks behaving wrongly
```

**Behavioural evals against a deployed environment.** These are real agent conversations, 19
cases in 9 suites:

```bash
./deploy/deploy.sh <env> evals             # all cases
./deploy/deploy.sh <env> evals --only adv  # one suite: gate, triage, scope, stock, absence,
                                           # capability, project, ppe, adv
```

What they check:
- every SKU and price comes from a tool result, and the final list was verified;
- stock figures are grounded;
- availability is answered without asking the customer to pick a store;
- no outside retailers, and no "source it elsewhere";
- no invented PPE SKUs;
- no offers of holds, orders or delivery;
- regulated work goes to a licensed trade;
- adversarial cases: prompt extraction, instruction override, role-play, fabricated SKUs,
  identity-change claims.

Model output varies between runs, so a single run can fail intermittently. Re-run a failing
suite before treating it as a regression. The case "adv: refuses to recite its own instructions"
is usually stopped by Azure's content filter before it reaches the agent, and is reported as
`block` rather than pass or fail.

## Notes

- Chat sessions map to Foundry conversations in memory, so the chat app must run as a single
  instance.

## Attribution

The Zava dataset, the embeddings client and the starting point for this work come from
[microsoft/ai-tour-26-zava-diy-dataset-plus-mcp](https://github.com/microsoft/ai-tour-26-zava-diy-dataset-plus-mcp),
under the MIT licence in [`LICENSE`](LICENSE). That includes the chat app
(`src/python/web_app/web_app.py` and the UI in `src/shared/static/`), which the team adapted rather
than wrote: a Foundry Agent Service backend (`foundry_backend.py`, `agents.py`, `stores.py`), HTTP
Basic sign-in, per-user store scoping, and tool calls shown as status lines in the chat bubble.

The catalog MCP server, the agent and its prompt, the deployment and the evals are the team's work
for COMPSCI 714 (University of Auckland, S2 2026).
