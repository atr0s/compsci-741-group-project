#!/usr/bin/env python3
"""
PostgreSQL provider for the Zava catalog MCP server.

Implements the retrieval arms (see README.md in this directory):
  - Semantic : text-embedding-3-small (1536-dim) over product descriptions
  - Keyword  : PostgreSQL full-text search with ts_rank
  - Filter   : structured SQL guard over price / category / stock

Every retrieval arm is batched: it takes a *list* of query terms and answers them
all in one SQL statement, using `unnest(...) WITH ORDINALITY` to number the
queries and a `LATERAL` join to run the per-query top-N against them. Costing a
project has one search per material line, so batching turns twenty round trips
into one and keeps the whole batch inside a single RLS transaction.

Two correctness rules are enforced throughout:

1. Vector comparisons ALWAYS use the cosine operator `<=>`.
   The ivfflat indexes are built with vector_cosine_ops and only accelerate
   `<=>`; `<->` (L2) and `<#>` (inner product) would bypass them.

2. RLS context is set with is_local=true inside an explicit transaction, so the
   setting is scoped to that transaction rather than leaking across a pooled
   connection to the next caller.

Requirements:
    - asyncpg
    - python-dotenv
"""

import asyncio
import json
import logging
import os
from typing import Any, Optional

import asyncpg
from dotenv import load_dotenv

load_dotenv(override=False)

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

POSTGRES_URL = os.getenv(
    "POSTGRES_URL", "postgresql://store_manager:StoreManager123!@db:5432/zava")

SCHEMA_NAME = "retail"
MAX_ROWS_CAP = 100

# Batched search: every arm accepts several query terms in one call, so costing a
# whole project takes one round trip per arm instead of one per material line.
MAX_QUERIES_PER_CALL = 20
# Total rows a batched call may return across all its queries. Twenty queries at
# max_rows=10 is already 200 product rows of JSON; past that the per-query
# allowance is trimmed rather than the batch refused.
MAX_BATCH_ROWS = 200

# Columns returned by every retrieval arm, so results line up across arms.
PRODUCT_SELECT = f"""
    p.product_id,
    p.sku,
    p.product_name,
    p.product_description,
    p.base_price AS price,
    c.category_name,
    pt.type_name,
    (SELECT COALESCE(SUM(i.stock_level), 0)
       FROM {SCHEMA_NAME}.inventory i
      WHERE i.product_id = p.product_id) AS total_stock
"""


def to_pgvector(embedding: list[float]) -> str:
    """Format a Python float list as a pgvector literal."""
    return "[" + ",".join(map(str, embedding)) + "]"


NO_MATCH_MESSAGE = "No matching products. Try a broader query or a lower threshold."


def _ok(results: list[dict[str, Any]], **extra: Any) -> str:
    """Build the LLM-friendly success envelope used by all tools."""
    payload: dict[str, Any] = {
        "results": results,
        "row_count": len(results),
        "columns": list(results[0].keys()) if results else [],
    }
    payload.update(extra)
    if not results:
        payload.setdefault("message", NO_MATCH_MESSAGE)
    return json.dumps(payload, indent=2, default=str)


def _ok_batch(queries: list[str], grouped: list[list[dict[str, Any]]], **extra: Any) -> str:
    """Build the success envelope for a multi-query search: one block per query.

    `columns` is reported once at the top level rather than repeated in every
    block - the rows all come from the same SELECT, and the repetition is pure
    token cost in the model's context.
    """
    searches: list[dict[str, Any]] = []
    columns: list[str] = []
    total = 0

    for query_text, rows in zip(queries, grouped, strict=True):
        block: dict[str, Any] = {
            "query": query_text,
            "results": rows,
            "row_count": len(rows),
        }
        if not rows:
            block["message"] = NO_MATCH_MESSAGE
        elif not columns:
            columns = list(rows[0].keys())
        searches.append(block)
        total += len(rows)

    payload: dict[str, Any] = {
        "searches": searches,
        "query_count": len(searches),
        "row_count": total,
        "columns": columns,
    }
    payload.update(extra)
    return json.dumps(payload, indent=2, default=str)


def _err(message: str) -> str:
    """Build the LLM-friendly error envelope used by all tools."""
    return json.dumps(
        {"error": message, "results": [], "row_count": 0, "columns": []},
        indent=2,
    )


def rows_per_query(max_rows: int, query_count: int) -> int:
    """Per-query row allowance, trimmed so a wide batch stays a readable result."""
    max_rows = min(max_rows, MAX_ROWS_CAP)
    if query_count <= 1:
        return max_rows
    return max(1, min(max_rows, MAX_BATCH_ROWS // query_count))


class PostgreSQLCatalog:
    """Retrieval arms and structured filtering for catalog product discovery."""

    def __init__(self, postgres_config: Optional[str] = None) -> None:
        self.postgres_config = postgres_config or POSTGRES_URL
        self.connection_pool: Optional[asyncpg.Pool] = None

    async def __aenter__(self) -> "PostgreSQLCatalog":
        return self

    async def __aexit__(self, exc_type: Optional[type], exc_val: Optional[Exception], exc_tb: Optional[object]) -> None:
        await self.close_pool()

    async def create_pool(self) -> None:
        """Create the connection pool."""
        if self.connection_pool is None:
            try:
                self.connection_pool = await asyncpg.create_pool(
                    self.postgres_config,
                    min_size=1,
                    max_size=3,
                    command_timeout=30,
                    server_settings={
                        "jit": "off",
                        "work_mem": "4MB",
                        "statement_timeout": "30s",
                    },
                )
                logger.info("✅ PostgreSQL connection pool created")
            except Exception as e:
                logger.error(f"❌ Failed to create PostgreSQL pool: {e}")
                raise

    async def close_pool(self) -> None:
        """Close the connection pool."""
        if self.connection_pool:
            await self.connection_pool.close()
            self.connection_pool = None
            logger.info("✅ PostgreSQL connection pool closed")

    async def _fetch(self, rls_user_id: str, query: str, *args: Any) -> list[dict[str, Any]]:
        """Run a query with transaction-scoped RLS context and return dict rows.

        set_config(..., true) makes the setting LOCAL to this transaction, so it
        cannot leak to the next user of this pooled connection.
        """
        if not self.connection_pool:
            raise RuntimeError(
                "No database connection pool available. Call create_pool() first.")

        async with self.connection_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.current_rls_user_id', $1, true)", rls_user_id)
                rows = await conn.fetch(query, *args)
                return [dict(row) for row in rows]

    @staticmethod
    def _regroup(rows: list[dict[str, Any]], query_count: int) -> list[list[dict[str, Any]]]:
        """Split a batched result set back into one row list per query, in order.

        `unnest(...) WITH ORDINALITY` numbers the queries from 1, and every row
        carries its query's ordinal in `query_index`; that bookkeeping column is
        removed here so the rows match the single-query shape exactly.
        """
        grouped: list[list[dict[str, Any]]] = [[] for _ in range(query_count)]
        for row in rows:
            index = int(row.pop("query_index")) - 1
            if 0 <= index < query_count:
                grouped[index].append(row)
        return grouped

    async def search_by_description_embeddings(
        self, embeddings: list[list[float]], queries: list[str], rls_user_id: str,
        max_rows: int = 10, similarity_threshold: float = 50.0,
    ) -> str:
        """Semantic search over product descriptions (1536-dim).

        Answers every query in `embeddings` in one statement. `queries` holds the
        original text of each, purely to label the blocks in the response, and
        must line up with `embeddings`.
        """
        rows_each = rows_per_query(max_rows, len(embeddings))
        distance_threshold = 1.0 - (similarity_threshold / 100.0)

        query = f"""
            WITH q AS (
                SELECT ord::int AS ord, vec_text::vector AS vec
                  FROM unnest($1::text[]) WITH ORDINALITY AS t(vec_text, ord)
            )
            SELECT q.ord AS query_index, s.*
              FROM q
              CROSS JOIN LATERAL (
                    SELECT {PRODUCT_SELECT},
                           (e.description_embedding <=> q.vec) AS distance
                      FROM {SCHEMA_NAME}.product_description_embeddings e
                      JOIN {SCHEMA_NAME}.products p       ON p.product_id  = e.product_id
                      JOIN {SCHEMA_NAME}.categories c     ON c.category_id = p.category_id
                      JOIN {SCHEMA_NAME}.product_types pt ON pt.type_id    = p.type_id
                     WHERE (e.description_embedding <=> q.vec) <= $3
                     ORDER BY e.description_embedding <=> q.vec
                     LIMIT $2
              ) s
             ORDER BY q.ord, s.distance
        """
        try:
            rows = await self._fetch(
                rls_user_id, query,
                [to_pgvector(embedding) for embedding in embeddings],
                rows_each, distance_threshold,
            )
            for row in rows:
                row["similarity_percent"] = round(
                    max(0.0, (1 - float(row["distance"])) * 100), 1)

            grouped = self._regroup(rows, len(queries))
            space = "text-embedding-3-small, 1536d"
            if len(queries) == 1:
                return _ok(grouped[0], space=space)
            return _ok_batch(queries, grouped, space=space, max_rows_per_query=rows_each)
        except Exception as e:
            return _err(f"Description embedding search failed: {e!s}")

    async def search_by_keywords(
        self, queries: list[str], rls_user_id: str, max_rows: int = 10,
    ) -> str:
        """Lexical arm: PostgreSQL full-text search ranked with ts_rank."""
        rows_each = rows_per_query(max_rows, len(queries))

        query = f"""
            WITH q AS (
                SELECT ord::int AS ord, term
                  FROM unnest($1::text[]) WITH ORDINALITY AS t(term, ord)
            )
            SELECT q.ord AS query_index, s.*
              FROM q
              CROSS JOIN LATERAL (
                    SELECT {PRODUCT_SELECT},
                           ts_rank(
                               to_tsvector('english', p.product_name || ' ' || p.product_description),
                               plainto_tsquery('english', q.term)
                           ) AS rank
                      FROM {SCHEMA_NAME}.products p
                      JOIN {SCHEMA_NAME}.categories c     ON c.category_id = p.category_id
                      JOIN {SCHEMA_NAME}.product_types pt ON pt.type_id    = p.type_id
                     WHERE to_tsvector('english', p.product_name || ' ' || p.product_description)
                           @@ plainto_tsquery('english', q.term)
                     ORDER BY rank DESC, p.product_name
                     LIMIT $2
              ) s
             ORDER BY q.ord, s.rank DESC
        """
        try:
            rows = await self._fetch(rls_user_id, query, queries, rows_each)
            grouped = self._regroup(rows, len(queries))
            arm = "keyword (full-text, ts_rank)"
            if len(queries) == 1:
                return _ok(grouped[0], arm=arm)
            return _ok_batch(queries, grouped, arm=arm, max_rows_per_query=rows_each)
        except Exception as e:
            return _err(f"Keyword search failed: {e!s}")

    async def filter_candidates(
        self, rls_user_id: str,
        product_ids: Optional[list[int]] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        category_name: Optional[str] = None,
        type_name: Optional[list[str]] = None,
        store_id: Optional[int] = None,
        in_stock_only: bool = False,
        max_rows: int = 20,
    ) -> str:
        """Structured SQL guard applied after retrieval.

        Every predicate is optional and parameterised. When store_id is given,
        stock is reported for that store only rather than summed across stores.
        """
        max_rows = min(max_rows, MAX_ROWS_CAP)

        stock_expr = f"""
            (SELECT COALESCE(SUM(i.stock_level), 0)
               FROM {SCHEMA_NAME}.inventory i
              WHERE i.product_id = p.product_id
                AND ($6::int IS NULL OR i.store_id = $6::int))
        """

        query = f"""
            SELECT p.product_id, p.sku, p.product_name, p.base_price AS price,
                   c.category_name, pt.type_name,
                   {stock_expr} AS total_stock
              FROM {SCHEMA_NAME}.products p
              JOIN {SCHEMA_NAME}.categories c     ON c.category_id = p.category_id
              JOIN {SCHEMA_NAME}.product_types pt ON pt.type_id    = p.type_id
             WHERE ($1::int[]   IS NULL OR p.product_id = ANY($1::int[]))
               AND ($2::numeric IS NULL OR p.base_price >= $2::numeric)
               AND ($3::numeric IS NULL OR p.base_price <= $3::numeric)
               AND ($4::text    IS NULL OR c.category_name ILIKE $4::text)
               AND ($5::text[]  IS NULL OR pt.type_name    ILIKE ANY($5::text[]))
               AND (NOT $7::boolean OR {stock_expr} > 0)
             ORDER BY p.base_price
             LIMIT $8
        """
        try:
            rows = await self._fetch(
                rls_user_id, query,
                product_ids, min_price, max_price,
                category_name, type_name, store_id, in_stock_only, max_rows,
            )
            return _ok(
                rows,
                filters_applied={
                    "product_ids": len(product_ids) if product_ids else None,
                    "min_price": min_price, "max_price": max_price,
                    "category_name": category_name, "type_name": type_name,
                    "store_id": store_id, "in_stock_only": in_stock_only,
                },
            )
        except Exception as e:
            return _err(f"Filter failed: {e!s}")

    async def get_stock_by_store(self, rls_user_id: str, product_ids: list[int],
                                 in_stock_only: bool = False) -> str:
        """Stock for each product at each store, instead of one collapsed total.

        filter_candidates answers "how much is there" - summed across stores, or
        one store's when store_id is given. It cannot say WHERE something is,
        which is the question a customer who has not named a store is asking.

        inventory is a full stores x products matrix, so this is 8 rows per
        product and needs no aggregation.
        """
        query = f"""
            SELECT p.product_id, p.sku, p.product_name,
                   s.store_id, s.store_name, s.is_online,
                   i.stock_level
              FROM {SCHEMA_NAME}.inventory i
              JOIN {SCHEMA_NAME}.stores s   ON s.store_id   = i.store_id
              JOIN {SCHEMA_NAME}.products p ON p.product_id = i.product_id
             WHERE i.product_id = ANY($1::int[])
               AND (NOT $2::boolean OR i.stock_level > 0)
             ORDER BY p.product_id, s.store_id
        """
        try:
            return _ok(await self._fetch(rls_user_id, query, product_ids, in_stock_only))
        except Exception as e:
            return _err(f"get_stock_by_store failed: {e!s}")

    async def get_stores(self, rls_user_id: str) -> str:
        """Return the store list, for grounding the store_id a customer picks.

        rls_user_id is deliberately not selected. It is the tenant key the RLS
        policy trusts, and the model has no business naming a value it could
        then be talked into passing back as its own identity.
        """
        query = f"""
            SELECT store_id, store_name, is_online
              FROM {SCHEMA_NAME}.stores
             ORDER BY store_id
        """
        try:
            return _ok(await self._fetch(rls_user_id, query))
        except Exception as e:
            return _err(f"Store lookup failed: {e!s}")

    async def get_taxonomy(self, rls_user_id: str) -> str:
        """Return the category/type taxonomy, for grounding queries and filters."""
        query = f"""
            SELECT c.category_name, pt.type_name, COUNT(p.product_id) AS product_count
              FROM {SCHEMA_NAME}.product_types pt
              JOIN {SCHEMA_NAME}.categories c ON c.category_id = pt.category_id
              LEFT JOIN {SCHEMA_NAME}.products p ON p.type_id = pt.type_id
             GROUP BY c.category_name, pt.type_name
             ORDER BY c.category_name, pt.type_name
        """
        try:
            return _ok(await self._fetch(rls_user_id, query))
        except Exception as e:
            return _err(f"Taxonomy lookup failed: {e!s}")


async def test_connection() -> bool:
    """Verify the database is reachable with the configured URL."""
    try:
        pool = await asyncpg.create_pool(POSTGRES_URL, min_size=1, max_size=1)
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        await pool.close()
        return True
    except Exception as e:
        logger.error(f"Connection test failed: {e}")
        return False


async def main() -> None:
    """Smoke-test the retrieval arms that need no external model."""
    if not await test_connection():
        print(f"❌ Cannot connect to PostgreSQL using: {POSTGRES_URL}")
        return

    async with PostgreSQLCatalog() as provider:
        await provider.create_pool()
        rls = "00000000-0000-0000-0000-000000000000"

        print("\n🔎 keyword search: 'cordless drill'")
        print(await provider.search_by_keywords(["cordless drill"], rls_user_id=rls, max_rows=5))

        print("\n🔎 batched keyword search: 3 terms in one call")
        print(await provider.search_by_keywords(
            ["exterior screws", "OSB sheathing", "safety glasses"], rls_user_id=rls, max_rows=3))

        print("\n🔎 filter: POWER TOOLS under $200, in stock")
        print(await provider.filter_candidates(
            rls_user_id=rls, category_name="POWER TOOLS", max_price=200, in_stock_only=True, max_rows=5))


if __name__ == "__main__":
    asyncio.run(main())
