#!/usr/bin/env python3
"""
Zava catalog MCP server: product discovery over the Zava DIY catalog.

Full documentation: README.md in this directory.

Tools
-----
  get_stores                    Stores and their ids
  get_taxonomy                  9 categories, 89 product types
  get_current_utc_date          Current UTC timestamp
  search_by_text                Semantic - search over description embeddings
  search_by_keyword             Lexical  - full-text search with ts_rank
  filter_candidates             Structured SQL guard (price / category / type / store / stock)
  get_stock_by_store            Stock for each product at every store

Design notes
------------
`filter_candidates` is exposed as an explicit tool rather than hidden inside
the search tools. That makes the agent's decisions - which arms to run, when to
relax a filter and retry - visible in the tool trace, which is the behaviour
worth demonstrating.

Every search tool takes either one query term or a LIST of them. Costing a project
means one search per material line, so a shed used to be forty-odd tool calls
and routinely exhausted the agent's tool rounds before the list was finished.
Batched, the same job is a handful of calls: the arms still appear separately in
the trace, but each one answers the whole materials list at once.

Usage:
    python zava_catalog.py --stdio --RLS_USER_ID=<uuid>
    python zava_catalog.py            # HTTP on :8000/mcp
"""

import argparse
import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from secrets import compare_digest
from typing import Annotated, Optional

import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from zava_catalog_postgres import MAX_QUERIES_PER_CALL, PostgreSQLCatalog

# Set from --RLS_USER_ID when running in stdio mode.
RLS_USER_ID: Optional[str] = None

DEFAULT_RLS_USER_ID = "00000000-0000-0000-0000-000000000000"


@dataclass
class AppContext:
    """Shared resources for the lifetime of the server."""

    db: PostgreSQLCatalog
    embeddings: object  # SemanticSearchTextEmbedding, or None if unconfigured


@asynccontextmanager
async def app_lifespan(_server: FastMCP) -> AsyncIterator[AppContext]:
    """Create the connection pool and embeddings client once, at startup."""
    db = PostgreSQLCatalog()

    # Azure OpenAI embeddings power search_by_text. Import lazily so the server still
    # starts if the customer_sales package is not on sys.path.
    embeddings = None
    try:
        import sys
        from pathlib import Path

        sibling = Path(__file__).parent.parent / "customer_sales"
        if sibling.exists() and str(sibling) not in sys.path:
            sys.path.insert(0, str(sibling))

        from customer_sales_semantic_search_text_embeddings import (
            SemanticSearchTextEmbedding,
        )

        embeddings = SemanticSearchTextEmbedding()
    except Exception as e:
        print(f"⚠️  Azure OpenAI embeddings unavailable: {e}")
        print("   search_by_text will be disabled; other tools still work.")

    await db.create_pool()

    try:
        yield AppContext(db=db, embeddings=embeddings)
    finally:
        try:
            await db.close_pool()
        except Exception as e:
            print(f"⚠️  Error closing database pool: {e}")


def _transport_security() -> Optional[TransportSecuritySettings]:
    """Let the public hostname past the SDK's DNS-rebinding guard.

    FastMCP turns that guard on automatically when its constructor host is
    localhost - which it always is here, because the FASTMCP_* env vars are not
    resolved (see run_http_server). Behind App Service the Host header is the
    site FQDN, so without this every request is answered 421.

    Returning None when MCP_ALLOWED_HOSTS is unset keeps local runs on the
    SDK's localhost-only default.
    """
    hosts = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if not hosts:
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"https://{h}" for h in hosts],
    )


mcp = FastMCP("mcp-zava-catalog",
              lifespan=app_lifespan, stateless_http=True,
              transport_security=_transport_security())


def get_header(ctx: Context, header_name: str) -> Optional[str]:
    """Extract a specific header from the request context."""
    request = ctx.request_context.request
    if request is not None and hasattr(request, "headers"):
        headers = request.headers
        if headers:
            header_value = headers.get(header_name)
            if header_value is not None:
                if isinstance(header_value, bytes):
                    return header_value.decode("utf-8")
                return str(header_value)
    return None


def get_rls_user_id(ctx: Context) -> str:
    """Resolve the Row Level Security user id for this request."""
    if RLS_USER_ID is not None:
        return RLS_USER_ID

    rls_user_id = get_header(ctx, "x-rls-user-id")
    return rls_user_id if rls_user_id is not None else DEFAULT_RLS_USER_ID


def get_app_context() -> AppContext:
    """Get the typed lifespan context."""
    ctx = mcp.get_context()
    app_context = ctx.request_context.lifespan_context
    if isinstance(app_context, AppContext):
        return app_context
    raise RuntimeError("Invalid lifespan context type")


def _err(message: str) -> str:
    """LLM-friendly error envelope."""
    return json.dumps(
        {"error": message, "results": [], "row_count": 0, "columns": []}, indent=2)


def query_terms(value: str | list[str]) -> list[str]:
    """Normalise a search argument into a clean list of query terms.

    Accepts a single term or a list of them. A string that looks like a JSON
    array is parsed as one: smaller models sometimes stringify the list instead
    of emitting it, and rejecting that costs a whole tool round to recover from.

    Blanks are dropped and repeats removed, so a model that lists the same
    material twice does not pay for it twice.
    """
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return query_terms(parsed)
        return [text] if text else []

    seen: dict[str, None] = {}
    for item in value:
        if isinstance(item, str) and item.strip():
            seen.setdefault(item.strip(), None)
    return list(seen)


def _too_many(tool: str, count: int) -> str:
    """Error envelope for a batch that exceeds the per-call query limit."""
    return _err(
        f"{tool} accepts at most {MAX_QUERIES_PER_CALL} terms per call, got {count}. "
        f"Split them across two calls.")


@mcp.tool()
async def search_by_text(
    ctx: Context,
    query: Annotated[str | list[str], Field(
        description="What the customer wants, in natural language, e.g. 'cordless drill for cabinet installation'. Pass a LIST to cover several materials in one call - ['exterior grade sheathing plywood', '2x4 framing lumber', 'galvanised deck screws'] - rather than calling this tool once per material.")],
    max_rows: Annotated[int, Field(
        description="Maximum number of products to return per query.")] = 10,
    similarity_threshold: Annotated[float, Field(
        description="Minimum similarity (0-100) for a product to be considered a match.")] = 50.0,
) -> str:
    """Semantic search over product descriptions (text-embedding-3-small, 1536-dim).

    Best for intent, specification and use-case queries, because product
    descriptions are written in functional language.

    Pass every material you need at once: the queries are embedded in a single
    API call and answered by a single SQL statement, so a list of twenty costs
    barely more than one and leaves the tool budget for the rest of the job.

    One term returns `results` ranked by cosine similarity, with
    similarity_percent. Several terms return `searches`, one block per query in
    the order given, each with its own `query` and `results`.
    """
    rls_user_id = get_rls_user_id(ctx)
    terms = query_terms(query)
    print(f"[search_by_text] queries={terms!r} rls={rls_user_id}")

    if not terms:
        return _err("search_by_text needs at least one non-empty query term.")
    if len(terms) > MAX_QUERIES_PER_CALL:
        return _too_many("search_by_text", len(terms))

    try:
        app = get_app_context()

        if app.embeddings is None or not app.embeddings.is_available():
            return _err(
                "Semantic search unavailable: Azure OpenAI is not configured. "
                "Set AZURE_OPENAI_ENDPOINT in .env. Try search_by_keyword instead.")

        embeddings = app.embeddings.generate_query_embeddings(terms)
        if not embeddings or len(embeddings) != len(terms):
            return _err("Failed to generate query embeddings. Please retry.")

        return await app.db.search_by_description_embeddings(
            embeddings, terms, rls_user_id=rls_user_id,
            max_rows=max_rows, similarity_threshold=similarity_threshold)
    except Exception as e:
        return _err(f"search_by_text failed: {e!s}")


@mcp.tool()
async def search_by_keyword(
    ctx: Context,
    query: Annotated[str | list[str], Field(
        description="Keywords or exact product terms, e.g. 'miter saw' or 'GFCI breaker'. Pass a LIST to look up several terms in one call - ['OSB', 'joist hanger', 'GFCI breaker'].")],
    max_rows: Annotated[int, Field(
        description="Maximum number of products to return per query.")] = 10,
) -> str:
    """Lexical full-text search over product names and descriptions.

    Use for exact terms, model names, and jargon where vector search may drift.
    Needs no external model, so it is the reliable fallback when Azure OpenAI is
    unavailable. Results are ranked by ts_rank.

    A list of terms is answered by one SQL statement, so look up the whole
    materials list in a single call rather than one term at a time.

    One term returns `results`; several return `searches`, one block per term in
    the order given.
    """
    rls_user_id = get_rls_user_id(ctx)
    terms = query_terms(query)
    print(f"[search_by_keyword] queries={terms!r}")

    if not terms:
        return _err("search_by_keyword needs at least one non-empty query term.")
    if len(terms) > MAX_QUERIES_PER_CALL:
        return _too_many("search_by_keyword", len(terms))

    try:
        app = get_app_context()
        return await app.db.search_by_keywords(terms, rls_user_id=rls_user_id, max_rows=max_rows)
    except Exception as e:
        return _err(f"search_by_keyword failed: {e!s}")


@mcp.tool()
async def filter_candidates(
    ctx: Context,
    product_ids: Annotated[Optional[list[int]], Field(
        description="Restrict to these product ids, typically the candidates the searches returned. Omit to search the whole catalogue.")] = None,
    min_price: Annotated[Optional[float], Field(
        description="Minimum base price.")] = None,
    max_price: Annotated[Optional[float], Field(
        description="Maximum base price.")] = None,
    category_name: Annotated[Optional[str], Field(
        description="Category filter, e.g. 'POWER TOOLS'. Case-insensitive; supports % wildcards.")] = None,
    type_name: Annotated[Optional[str | list[str]], Field(
        description="Product type filter, e.g. 'HAMMERS'. Case-insensitive; supports % wildcards. Pass a LIST to pull several types in one call - ['SCREWS', 'NAILS', 'DIMENSIONAL LUMBER'] - which is the cheapest and most reliable way to cover a known materials list, since a type lookup cannot miss the way a semantic search can. Raise max_rows to cover them all.")] = None,
    store_id: Annotated[Optional[int], Field(
        description="Report stock for this store only (1-8) instead of summing across stores.")] = None,
    in_stock_only: Annotated[bool, Field(
        description="Only return products with stock above zero.")] = False,
    max_rows: Annotated[int, Field(
        description="Maximum number of products to return.")] = 20,
) -> str:
    """Apply structured constraints that vector search cannot express.

    Vector similarity cannot represent "under $200 and in stock at Kirkland".
    Run this after fusing to enforce those constraints.

    If the result comes back empty, relax and retry rather than reporting failure:
    widen the price band, drop the store restriction, or re-run the search with a
    larger max_rows so more candidates survive filtering.
    """
    rls_user_id = get_rls_user_id(ctx)
    print(f"[filter_candidates] ids={len(product_ids) if product_ids else 0} "
          f"price=({min_price},{max_price}) store={store_id}")

    try:
        app = get_app_context()
        # One type or many: the SQL matches with ILIKE ANY either way, so the
        # model can cover a whole materials list in a single deterministic call.
        types = query_terms(type_name) if type_name is not None else None
        return await app.db.filter_candidates(
            rls_user_id=rls_user_id, product_ids=product_ids,
            min_price=min_price, max_price=max_price,
            category_name=category_name, type_name=types or None,
            store_id=store_id, in_stock_only=in_stock_only, max_rows=max_rows)
    except Exception as e:
        return _err(f"filter_candidates failed: {e!s}")


@mcp.tool()
async def get_taxonomy(ctx: Context) -> str:
    """List the 9 categories and 89 product types, with product counts.

    Use this to ground category_name and type_name filter values in the real
    taxonomy instead of guessing. Note that 'HAND TOOLS' is both a category and
    a product type (under GARDEN & OUTDOOR), so always pair type with category.
    """
    rls_user_id = get_rls_user_id(ctx)
    print("[get_taxonomy]")

    try:
        app = get_app_context()
        return await app.db.get_taxonomy(rls_user_id=rls_user_id)
    except Exception as e:
        return _err(f"get_taxonomy failed: {e!s}")


@mcp.tool()
async def get_stock_by_store(
    ctx: Context,
    product_ids: Annotated[list[int], Field(
        description="The products to locate, normally the ones you are about to show. At most 25 per call.")],
    in_stock_only: Annotated[bool, Field(
        description="Return only the stores that actually hold stock, so 'where can I get this?' is not answered with a list of zeros.")] = False,
) -> str:
    """Where a product is stocked, store by store.

    `filter_candidates` answers "how much is there" as a single number: summed
    across all 8 stores, or one store's if you passed `store_id`. This answers
    "WHERE is it", which is what a customer who has not named a store is really
    asking.

    Use it instead of asking the customer to name a store just so you can check
    availability - that request is the check. Returns one row per product per
    store with `stock_level`, so a product absent from one store is visible as a
    zero rather than as a missing row (unless in_stock_only is set).
    """
    rls_user_id = get_rls_user_id(ctx)
    print(f"[get_stock_by_store] ids={len(product_ids) if product_ids else 0}")

    if not product_ids:
        return _err("get_stock_by_store needs at least one product_id.")
    if len(product_ids) > 25:
        return _err(
            f"get_stock_by_store accepts at most 25 products per call, got {len(product_ids)}. "
            f"Split them across two calls.")

    try:
        app = get_app_context()
        return await app.db.get_stock_by_store(
            rls_user_id=rls_user_id, product_ids=product_ids, in_stock_only=in_stock_only)
    except Exception as e:
        return _err(f"get_stock_by_store failed: {e!s}")


@mcp.tool()
async def get_stores(ctx: Context) -> str:
    """List the Zava stores and the `store_id` each one takes.

    Call this before offering a customer a choice of store, so the id you carry
    into `filter_candidates` is the catalogue's own rather than a remembered
    one. A wrong `store_id` is the worst kind of error here: it reports another
    store's stock, and nothing about the answer looks wrong.

    `is_online` marks the store that serves a customer who is not shopping in
    person - the sensible default when they have not said where they are.
    """
    rls_user_id = get_rls_user_id(ctx)
    print("[get_stores]")

    try:
        app = get_app_context()
        return await app.db.get_stores(rls_user_id=rls_user_id)
    except Exception as e:
        return _err(f"get_stores failed: {e!s}")


@mcp.tool()
async def get_current_utc_date() -> str:
    """Get the current UTC date and time in ISO format.

    Returns:
        Current UTC date and time in ISO format (YYYY-MM-DDTHH:MM:SS.fffffZ)
    """
    print("Retrieving current UTC date and time")
    try:
        return f"Current UTC Date/Time: {datetime.now(timezone.utc).isoformat()}"
    except Exception as e:
        return f"Error retrieving current UTC date: {e!s}"


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Require a shared secret on every request except the health probe.

    Foundry's run loop calls this endpoint outbound from Microsoft
    infrastructure, so it has to be publicly reachable. RLS is no defence at
    this layer - x-rls-user-id is chosen by the caller - which makes this the
    only thing between the database and the internet.
    """

    def __init__(self, app, api_key: str) -> None:
        super().__init__(app)
        self._api_key = api_key

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        if not compare_digest(request.headers.get("x-api-key", ""), self._api_key):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


async def _healthz(_request: Request) -> PlainTextResponse:
    """Unauthenticated liveness probe, so warmup pings don't log as 401s."""
    return PlainTextResponse("ok")


async def run_http_server() -> None:
    """Run the MCP server in HTTP mode."""
    app = mcp.streamable_http_app()
    app.router.routes.append(Route("/healthz", _healthz, methods=["GET"]))

    api_key = os.getenv("MCP_API_KEY")
    if api_key:
        app.add_middleware(ApiKeyMiddleware, api_key=api_key)
    else:
        print("⚠️  MCP_API_KEY unset - the endpoint is UNAUTHENTICATED.")

    # Read the bind address here rather than trusting mcp.settings: FastMCP's
    # pydantic-settings env resolution does not pick up FASTMCP_* under this
    # Python/pydantic pairing, so it silently keeps the 127.0.0.1:8000 default.
    # PORT comes first because App Service assigns it.
    host = os.getenv("FASTMCP_HOST") or mcp.settings.host
    port = int(os.getenv("PORT") or os.getenv("FASTMCP_PORT") or mcp.settings.port)

    print(f"📡 MCP endpoint available at: http://{host}:{port}/mcp")
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    await uvicorn.Server(config).serve()


def main() -> None:
    """Main entry point for the MCP server."""
    global RLS_USER_ID

    parser = argparse.ArgumentParser()
    parser.add_argument("--stdio", action="store_true",
                        help="Run server in stdio mode")
    parser.add_argument("--RLS_USER_ID", type=str,
                        default=None, help="Row Level Security User ID")
    args = parser.parse_args()

    RLS_USER_ID = args.RLS_USER_ID

    if args.stdio:
        mcp.run()
    else:
        asyncio.run(run_http_server())


if __name__ == "__main__":
    main()
