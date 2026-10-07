# Zava catalog MCP server

The tool tier of the Zava DIY Assistant. It is a Model Context Protocol server that the Foundry
agent `zava-diy-advisor` calls to find products, check prices and stock, and verify its answer.
The model never writes SQL. It picks a tool and supplies arguments, and the server runs fixed,
parameterised queries against the `retail` schema in PostgreSQL.

| File | Role |
|---|---|
| `zava_catalog.py` | MCP server: tool definitions, argument handling, API-key middleware, HTTP and stdio entry points |
| `zava_catalog_postgres.py` | PostgreSQL provider: the SQL behind each tool, connection pool, row-level-security context |
| `../customer_sales/customer_sales_semantic_search_text_embeddings.py` | Azure OpenAI embeddings client, reused from the upstream Zava sample |

## Why a new server

The upstream Zava sample ships three MCP servers. None fitted a customer-facing product advisor:

| Upstream server | Tools | Problem for this use case |
|---|---|---|
| `customer_sales` | `get_products_by_name` (`ILIKE` match), `get_current_utc_date` | One name-match search; results returned as a text dump |
| `customer_sales_semantic_search` | `semantic_search_products`, `get_current_utc_date` | One semantic search, one query per call; no price, type or stock filtering |
| `sales_analysis` | `get_multiple_table_schemas`, `execute_sales_query` | Runs any SQL the model writes, including against customers and orders |

This server replaces them with seven read-only tools designed for the agent's workflow: scope,
search, filter, verify.

## Tools

All tools return JSON. Each is listed with its arguments and what it returns.

### Grounding

**`get_stores()`**
- Returns the stores as `store_id`, `store_name` and `is_online`.
- Use it so the store the agent filters on is the catalogue's own id rather than a guess.
- The `rls_user_id` column is deliberately not returned. The model never sees the identity the
  database trusts, so it can't be talked into passing it back.

**`get_taxonomy()`**
- Returns the 9 categories and 89 product types, with a product count for each.
- Use it to ground `category_name` and `type_name` filter values in the real taxonomy.

**`get_current_utc_date()`**
- Returns the current UTC timestamp.

### Retrieval

**`search_by_text(query, max_rows=10, similarity_threshold=50)`**
- Semantic search. Each query is embedded with `text-embedding-3-small` (1536 dimensions) and
  compared with the product description embeddings by cosine distance (`<=>`).
- Products below `similarity_threshold` (0–100) are dropped.
- Each result row includes `similarity_percent`.
- Best for intent and use-case queries, for example "something to cut 50mm PVC".

**`search_by_keyword(query, max_rows=10)`**
- PostgreSQL full-text search (`to_tsvector` / `plainto_tsquery`) over product name and
  description, ranked by `ts_rank`.
- Best for exact terms, model names and trade jargon.
- It needs no external model, so it keeps working if Azure OpenAI is unavailable.

Both search tools accept **one term or a list of up to 20**. A list is embedded in one API call
and answered in one SQL statement: `unnest(...) WITH ORDINALITY` numbers the queries, and a
`LATERAL` join runs the top-N for each. A single term returns `results`. A list returns
`searches`, one block per term in the order given.

### Stock and verification

**`filter_candidates(product_ids, min_price, max_price, category_name, type_name, store_id, in_stock_only=False, max_rows=20)`**
- Applies the constraints vector search can't express: price range, category, product type,
  store and stock. Every argument is optional.
- `type_name` accepts a list, matched with `ILIKE ANY`, so a known materials list can be pulled
  by type in one call. A type look-up can't miss a product the way a semantic search can.
- With `store_id`, stock is reported for that store only; otherwise it is summed across all
  8 stores.
- The agent also calls it at the end, with the final list's `product_ids`, to verify that every
  product is real and to re-read its price and stock before answering.
- Results are ordered by price. The filters applied are echoed back in `filters_applied`.

**`get_stock_by_store(product_ids, in_stock_only=False)`**
- Returns stock for up to 25 products at every store, one row per product per store.
- It answers "where can I get this?" without the agent asking the customer to pick a store
  first.
- A store with no stock shows as a zero, unless `in_stock_only` is set.

### Columns returned

- **Search tools:** `product_id`, `sku`, `product_name`, `product_description`, `price` (the
  retail `base_price`), `category_name`, `type_name` and `total_stock`.
- **`filter_candidates`:** the same columns without the description.
- **Never returned:** the `cost` and `gross_margin_percent` columns of `retail.products`. No tool
  selects them. No tool reads `customers`, `orders` or `order_items`.

## Response format

Every tool returns the same envelope, so the model handles every result the same way:

```json
{ "results": [...], "row_count": 3, "columns": ["product_id", "sku", "..."] }
```

- **Empty result:** carries a `message` suggesting a broader query or a lower threshold.
- **Errors:** come back as `{"error": "...", "results": [], ...}` rather than as exceptions, so a
  failed call doesn't end the agent's turn. The model reads the error and tries another route.
- **Search unavailable:** if the embeddings client can't be configured, the server still starts.
  `search_by_text` then returns an error naming `search_by_keyword` as the fallback.

## Limits

| Constant | Value | Why |
|---|---|---|
| `MAX_QUERIES_PER_CALL` | 20 | Terms per batched search |
| `MAX_ROWS_CAP` | 100 | Rows per query, whatever `max_rows` the model asks for |
| `MAX_BATCH_ROWS` | 200 | Rows per batched call; the per-term allowance shrinks as the batch grows |
| `get_stock_by_store` | 25 products | Products per call |
| Statement timeout | 30 s | Per SQL statement |
| Connection pool | 1–3 connections | One small App Service instance |

These limits are cost controls as much as safety controls. Every row returned is re-sent to the
model on each later call in the conversation, so result size multiplies through the whole turn.

## Input handling

- **List or string.** A query can be a string or a list. A list that arrives as a JSON string,
  which smaller models sometimes send, is parsed rather than rejected, because rejecting it costs
  a whole tool round.
- **Clean-up.** Blank and repeated terms are dropped.
- **Parameters only.** Model-supplied values are only ever SQL parameters (`$1`, `$2`, …). Table
  and column names are fixed in the code.

## Security

**Authentication.**
- Every HTTP request except `GET /healthz` must carry the shared secret in `x-api-key`. It is
  compared in constant time (`secrets.compare_digest`), and a wrong or missing key gets `401`.
- The endpoint has to be public, because Foundry calls it from Microsoft's infrastructure. That
  makes the key the main control in front of the database.

**Host checking.** `MCP_ALLOWED_HOSTS` turns on the MCP SDK's DNS-rebinding protection for the
App Service hostname.

**Row-level security.**
- The caller's identity arrives in `x-rls-user-id`. Foundry fills it from a per-turn input, so
  the model never sees it.
- Each tool call runs in its own transaction, which starts with
  `set_config('app.current_rls_user_id', $1, true)`. The `true` makes the setting local to the
  transaction, so it can't leak to the next request on a pooled connection.
- If the header is missing, the server falls back to the all-stores identity. That is acceptable
  only because every tool reads catalogue data.

**Least privilege.** The server connects as `store_manager`, which owns nothing and does not
bypass RLS (`deploy/sql/02_bootstrap.sql`).

**Known weakness.** The tool allow-list is enforced by Foundry, per agent, not by this server.
Anyone holding the key can call any of the seven tools, as any identity. The fix is to enforce an
allow-list per caller here, and to replace the plain `x-rls-user-id` header with a short-lived
identity token signed by the chat app and verified by this server.

## Configuration

| Variable | Purpose |
|---|---|
| `POSTGRES_URL` | Connection string for the `store_manager` role |
| `MCP_API_KEY` | Shared secret required in `x-api-key`. If unset, the server warns and runs **unauthenticated**; local use only |
| `AZURE_OPENAI_ENDPOINT` | Foundry / Azure OpenAI endpoint for query embeddings, reached using the app's managed identity |
| `EMBEDDING_MODEL_DEPLOYMENT_NAME` | Embedding deployment, default `text-embedding-3-small` |
| `MCP_ALLOWED_HOSTS` | Comma-separated public hostnames allowed past DNS-rebinding protection |
| `PORT` / `FASTMCP_HOST` / `FASTMCP_PORT` | Bind address. App Service sets `PORT` |

`deploy/infra/modules/appservice.bicep` sets all of these in Azure.

## Running

```bash
# HTTP (streamable MCP at /mcp), as on App Service
python zava_catalog.py

# stdio, for a local MCP client, with a fixed RLS identity
python zava_catalog.py --stdio --RLS_USER_ID=00000000-0000-0000-0000-000000000000

# Smoke-test the SQL behind the tools that need no external model
python zava_catalog_postgres.py
```

On App Service, the startup command is `python catalog/zava_catalog.py`, and
`deploy/scripts/package.py` builds the zip. The server is stateless (`stateless_http=True`), so
any instance can answer any request.
