"""MCP Server for the Zava sales agent.

Exposes read-only MCP tools over streamable HTTP for:
  * Catalog: semantic_search_products, get_product_details
  * Sales content: search_kb_articles, search_case_studies, get_pricing,
    compare_plans
  * Analytics escape hatch: get_table_schemas, execute_sales_query
  * Utility: get_current_utc_date

Connects to Postgres + pgvector via asyncpg and to Azure OpenAI embeddings
via Entra ID. Vector lookups go through an in-memory LRU cache keyed on
(model_name, query_text) so repeated searches in a session are free.

Tables that don't exist in the database (e.g. before the redesigned
`generate_database.py` is run) cause their corresponding tools to return a
graceful "not yet provisioned" message instead of a 500. This lets the
agent keep working while the schema is being built out.
"""

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import quote

import asyncpg
import uvicorn
from dotenv import load_dotenv

# Load environment variables from .env.local (for local development)
load_dotenv(Path(__file__).parent.parent / ".env.local")

# Wire Azure Monitor OpenTelemetry only when deployed (env var is set by infra).
if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(logger_name=__name__)
    except Exception as exc:  # pragma: no cover — best-effort
        logging.getLogger(__name__).warning("App Insights setup failed: %s", exc)

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from openai import AsyncAzureOpenAI
from pydantic import Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Globals populated in lifespan.
db_provider: Optional["PostgreSQLProvider"] = None
embedding_provider: Optional["EmbeddingProvider"] = None


_DSN_RE = re.compile(r"^([^:]+://)([^:/@]+):([^@]+)@(.+)$")


def _safe_dsn(url: str) -> str:
    """Re-encode userinfo so asyncpg can parse DSNs with special chars in passwords."""
    m = _DSN_RE.match(url)
    if not m:
        return url
    scheme, user, password, rest = m.groups()
    return f"{scheme}{quote(user, safe='')}:{quote(password, safe='')}@{rest}"


# ---- Postgres provider ---------------------------------------------------
class PostgreSQLProvider:
    def __init__(self, dsn: str):
        self.dsn = _safe_dsn(dsn)
        self.pool: Optional[asyncpg.Pool] = None
        # Cache of "table exists?" lookups so we don't probe every call.
        self._table_exists: dict[str, bool] = {}

    async def connect(self):
        try:
            self.pool = await asyncpg.create_pool(dsn=self.dsn, min_size=1, max_size=10)
            logger.info("✅ PostgreSQL connection pool established")
        except Exception as e:
            logger.error("❌ Failed to connect to PostgreSQL: %s", e)
            raise

    async def close(self):
        if self.pool:
            await self.pool.close()
            logger.info("PostgreSQL connection pool closed")

    async def execute_query(self, query: str, *args) -> list[dict]:
        if not self.pool:
            await self.connect()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
            return [dict(row) for row in rows]

    async def table_exists(self, schema: str, table: str) -> bool:
        key = f"{schema}.{table}"
        if key in self._table_exists:
            return self._table_exists[key]
        if not self.pool:
            await self.connect()
        async with self.pool.acquire() as conn:
            exists = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = $1 AND table_name = $2)",
                schema, table,
            )
        self._table_exists[key] = bool(exists)
        return self._table_exists[key]

    async def get_table_schemas(self) -> str:
        if not self.pool:
            await self.connect()

        schema_query = """
        SELECT table_name, column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'retail'
        ORDER BY table_name, ordinal_position;
        """

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(schema_query)
            tables: dict[str, list] = {}
            for row in rows:
                tables.setdefault(row["table_name"], []).append(
                    {
                        "column": row["column_name"],
                        "type": row["data_type"],
                        "nullable": row["is_nullable"] == "YES",
                        "default": row["column_default"],
                    }
                )
            return json.dumps(tables, indent=2)


# ---- Embedding provider with in-memory LRU cache ------------------------
class EmbeddingProvider:
    """Azure OpenAI embeddings with a model-name-keyed cache.

    The cache key includes the deployment/model name, so swapping
    deployments invalidates the cache automatically. Bounded at 512 entries
    with simple FIFO eviction (good enough for chat session traffic).
    """

    _CACHE_MAX = 512

    def __init__(self, openai_endpoint: str, embedding_deployment: str, api_version: str):
        self.openai_endpoint = openai_endpoint
        self.embedding_deployment = embedding_deployment

        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )

        self.client = AsyncAzureOpenAI(
            api_version=api_version,
            azure_endpoint=openai_endpoint,
            azure_ad_token_provider=token_provider,
        )
        # Cache: { (model_name, text) -> [float] }
        self._cache: dict[tuple[str, str], list[float]] = {}
        # Async lock so concurrent identical lookups don't both call AOAI.
        self._lock = asyncio.Lock()
        logger.info(
            "✅ Embedding provider ready (endpoint=%s, deployment=%s)",
            openai_endpoint, embedding_deployment,
        )

    async def get_embedding(self, text: str) -> list[float]:
        key = (self.embedding_deployment, text)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            response = await self.client.embeddings.create(
                input=text, model=self.embedding_deployment
            )
            embedding = response.data[0].embedding
            # Bounded FIFO eviction.
            if len(self._cache) >= self._CACHE_MAX:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = embedding
            return embedding


# ---- Lifespan -----------------------------------------------------------
@asynccontextmanager
async def lifespan(mcp_server: FastMCP):
    global db_provider, embedding_provider

    logger.info("🚀 Starting MCP server initialization...")

    postgres_url = os.getenv("POSTGRES_URL")
    if postgres_url:
        db_provider = PostgreSQLProvider(postgres_url)
        await db_provider.connect()
    else:
        logger.warning("⚠️  POSTGRES_URL not set — database tools will not work")

    openai_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    embedding_deployment = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")

    if openai_endpoint:
        try:
            embedding_provider = EmbeddingProvider(openai_endpoint, embedding_deployment, api_version)
        except Exception as e:
            logger.error("Failed to initialise embeddings: %s", e)
    else:
        logger.warning("⚠️  AZURE_OPENAI_ENDPOINT not set — semantic search will not work")

    logger.info("✅ MCP server ready")
    try:
        yield
    finally:
        logger.info("🛑 Shutting down MCP server...")
        if db_provider:
            await db_provider.close()


mcp = FastMCP("Zava Sales Agent Tools", lifespan=lifespan)


# ---- SQL guardrails ------------------------------------------------------
_SQL_FORBIDDEN = (
    "--", "/*",
    "DROP ", "DELETE ", "INSERT ", "UPDATE ", "ALTER ", "CREATE ",
    "TRUNCATE ", "GRANT ", "REVOKE ", "EXEC ", "EXECUTE ", "MERGE ",
    "CALL ", "COPY ",
)


def validate_sql_query(query: str) -> None:
    """Raise ToolError unless `query` is a single SELECT with no banned patterns."""
    normalized = query.strip()
    if normalized.endswith(";"):
        normalized = normalized[:-1].strip()
    upper = normalized.upper()

    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        raise ToolError("Only SELECT queries are allowed")
    if ";" in normalized:
        raise ToolError("Multiple SQL statements are not allowed")
    for pat in _SQL_FORBIDDEN:
        if pat in upper:
            raise ToolError(f"Query contains forbidden pattern: {pat.strip()}")


def _embedding_to_pg(vec: list[float]) -> str:
    return "[" + ",".join(map(str, vec)) + "]"


# ---- MCP Tools -----------------------------------------------------------
@mcp.tool(annotations={"title": "Get Current UTC Date", "readOnlyHint": True, "openWorldHint": False})
def get_current_utc_date() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


@mcp.tool(annotations={"title": "Get Database Table Schemas", "readOnlyHint": True, "openWorldHint": False})
async def get_table_schemas(ctx: Context) -> str:
    """Return JSON describing the columns of every table in the `retail` schema."""
    if not db_provider:
        raise ToolError("Database not configured. Set POSTGRES_URL.")
    try:
        await ctx.info("Fetching database table schemas...")
        return await db_provider.get_table_schemas()
    except Exception as e:
        await ctx.error(f"Error getting schemas: {e}")
        raise ToolError(f"Failed to get table schemas: {e}")


@mcp.tool(annotations={"title": "Execute Sales Query", "readOnlyHint": True, "openWorldHint": False})
async def execute_sales_query(
    query: Annotated[str, Field(description="SQL SELECT query against the 'retail' schema.")],
    ctx: Context,
) -> str:
    """Execute a read-only SQL query and return the results as JSON.

    For ad-hoc analytics. Prefer the dedicated tools (semantic_search_products,
    search_case_studies, search_kb_articles, get_pricing) for the common cases.
    """
    if not db_provider:
        raise ToolError("Database not configured. Set POSTGRES_URL.")

    validate_sql_query(query)
    try:
        await ctx.info(f"Executing query: {query[:100]}...")
        results = await db_provider.execute_query(query)
        await ctx.info(f"Query returned {len(results)} rows")
        return json.dumps(results, indent=2, default=str)
    except Exception as e:
        await ctx.error(f"Error executing query: {e}")
        raise ToolError(f"Query execution failed: {e}")


@mcp.tool(annotations={"title": "Semantic Product Search", "readOnlyHint": True, "openWorldHint": True})
async def semantic_search_products(
    query: Annotated[str, Field(description="What kind of product to find")],
    ctx: Context,
    max_rows: Annotated[int, Field(description="Maximum results", ge=1, le=20)] = 5,
    threshold: Annotated[float, Field(description="Minimum similarity (0-1)", ge=0, le=1)] = 0.5,
) -> str:
    """Find products by semantic similarity using pgvector cosine distance.

    Returns a Markdown-formatted list with `[doc-id]` style citations the
    validation middleware can verify.
    """
    if not embedding_provider:
        raise ToolError("Semantic search not configured. Set AZURE_OPENAI_ENDPOINT.")
    if not db_provider or not db_provider.pool:
        raise ToolError("Database not connected. Set POSTGRES_URL.")

    await ctx.info(f"Embedding query: {query[:60]}...")
    vec = await embedding_provider.get_embedding(query)
    pg_vec = _embedding_to_pg(vec)

    sql = """
    SELECT
        p.product_id,
        p.product_name,
        p.product_description,
        c.category_name,
        p.base_price,
        1 - (de.description_embedding <=> $1::vector) AS similarity
    FROM retail.products p
    JOIN retail.categories c ON p.category_id = c.category_id
    JOIN retail.product_description_embeddings de ON p.product_id = de.product_id
    WHERE 1 - (de.description_embedding <=> $1::vector) > $2
    ORDER BY similarity DESC
    LIMIT $3;
    """

    async with db_provider.pool.acquire() as conn:
        rows = await conn.fetch(sql, pg_vec, threshold, max_rows)
    if not rows:
        return f"No products found matching '{query}' with similarity > {threshold}."

    lines = []
    for r in rows:
        lines.append(
            f"- **[product-{r['product_id']}]** {r['product_name']} "
            f"({r['category_name']}) — ${r['base_price']:.2f} "
            f"(similarity: {r['similarity']:.0%})\n  "
            f"{(r['product_description'] or '')[:140]}"
        )
    return "\n".join(lines)


@mcp.tool(annotations={"title": "Get Product Details", "readOnlyHint": True, "openWorldHint": False})
async def get_product_details(
    product_id: Annotated[int, Field(description="Numeric product_id, e.g. 42")],
    ctx: Context,
) -> str:
    """Return full details for one product (description, price, category, stock)."""
    if not db_provider:
        raise ToolError("Database not configured.")
    sql = """
    SELECT p.product_id, p.product_name, p.product_description, p.base_price,
           c.category_name
    FROM retail.products p
    JOIN retail.categories c ON p.category_id = c.category_id
    WHERE p.product_id = $1
    LIMIT 1;
    """
    rows = await db_provider.execute_query(sql, product_id)
    if not rows:
        return f"No product found with id {product_id}."
    return json.dumps(rows[0], indent=2, default=str)


@mcp.tool(annotations={"title": "Search Case Studies", "readOnlyHint": True, "openWorldHint": True})
async def search_case_studies(
    query: Annotated[str, Field(description="What kind of customer story to find")],
    ctx: Context,
    industry: Annotated[Optional[str], Field(description="Filter by industry, e.g. 'manufacturing'")] = None,
    min_team_size: Annotated[Optional[int], Field(description="Minimum team size to filter on")] = None,
    max_rows: Annotated[int, Field(description="Maximum results", ge=1, le=10)] = 3,
) -> str:
    """Semantic-search Zava case studies, optionally filtered by industry / team size.

    Returns Markdown with `[case-…]` citations. Falls back gracefully if the
    `case_studies` table hasn't been provisioned yet.
    """
    if not db_provider:
        raise ToolError("Database not configured.")
    if not await db_provider.table_exists("retail", "case_studies"):
        return (
            "Case study library is not provisioned yet. "
            "Run `python data/generate_database.py` to seed the `retail.case_studies` table."
        )
    if not embedding_provider:
        raise ToolError("Semantic search not configured. Set AZURE_OPENAI_ENDPOINT.")

    vec = await embedding_provider.get_embedding(query)
    pg_vec = _embedding_to_pg(vec)

    where_extra = []
    args: list = [pg_vec]
    if industry:
        # Forgiving match: lowercase, allow space/underscore/dash equivalence,
        # and substring match so "property management" hits "property_management".
        normalised = re.sub(r"[\s_-]+", "_", industry.strip().lower())
        args.append(f"%{normalised}%")
        where_extra.append(
            f"AND regexp_replace(lower(industry), '[\\s_-]+', '_', 'g') ILIKE ${len(args)}"
        )
    if min_team_size is not None:
        args.append(min_team_size)
        where_extra.append(f"AND team_size >= ${len(args)}")
    args.append(max_rows)

    sql = f"""
    SELECT case_id, title, industry, team_size, summary,
           1 - (embedding <=> $1::vector) AS similarity
    FROM retail.case_studies
    WHERE 1=1 {' '.join(where_extra)}
    ORDER BY embedding <=> $1::vector
    LIMIT ${len(args)};
    """
    async with db_provider.pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    if not rows:
        return f"No case studies matched '{query}'."
    lines = []
    for r in rows:
        lines.append(
            f"- **[case-{r['case_id']}]** {r['title']} — {r['industry']}, "
            f"{r['team_size']} people. (similarity: {r['similarity']:.0%})\n  {r['summary'] or ''}"
        )
    return "\n".join(lines)


@mcp.tool(annotations={"title": "Search Knowledge Base Articles", "readOnlyHint": True, "openWorldHint": True})
async def search_kb_articles(
    query: Annotated[str, Field(description="What article to find")],
    ctx: Context,
    max_rows: Annotated[int, Field(description="Maximum results", ge=1, le=10)] = 3,
) -> str:
    """Semantic search over Zava sales / how-Zava-works KB articles."""
    if not db_provider:
        raise ToolError("Database not configured.")
    if not await db_provider.table_exists("retail", "sales_kb_articles"):
        return (
            "KB article library is not provisioned yet. "
            "Run `python data/generate_database.py` to seed the `retail.sales_kb_articles` table."
        )
    if not embedding_provider:
        raise ToolError("Semantic search not configured.")

    vec = await embedding_provider.get_embedding(query)
    pg_vec = _embedding_to_pg(vec)

    sql = """
    SELECT article_id, title, body, category,
           1 - (embedding <=> $1::vector) AS similarity
    FROM retail.sales_kb_articles
    ORDER BY embedding <=> $1::vector
    LIMIT $2;
    """
    rows = await db_provider.execute_query(sql, pg_vec, max_rows)
    if not rows:
        return "No articles found."
    lines = []
    for r in rows:
        lines.append(
            f"- **[kb-{r['article_id']}]** {r['title']} ({r['category']}) "
            f"(similarity: {r['similarity']:.0%})\n  {(r['body'] or '')[:200]}…"
        )
    return "\n".join(lines)


@mcp.tool(annotations={"title": "Get Pricing", "readOnlyHint": True, "openWorldHint": False})
async def get_pricing(
    ctx: Context,
    plan_name: Annotated[Optional[str], Field(description="Name of one plan, e.g. 'Pro'. Omit to list all.")] = None,
) -> str:
    """Return pricing plans. With no plan_name, returns all plans."""
    if not db_provider:
        raise ToolError("Database not configured.")
    if not await db_provider.table_exists("retail", "pricing_plans"):
        return (
            "Pricing plans are not provisioned yet. "
            "Run `python data/generate_database.py` to seed the `retail.pricing_plans` table."
        )
    if plan_name:
        sql = "SELECT plan_id, plan_name, monthly_price, annual_price, features FROM retail.pricing_plans WHERE lower(plan_name) = lower($1) LIMIT 1;"
        rows = await db_provider.execute_query(sql, plan_name)
        if not rows:
            return f"No plan named '{plan_name}'."
        r = rows[0]
        return json.dumps(
            {
                "doc_id": f"plan-{r['plan_id']}",
                "plan_name": r["plan_name"],
                "monthly_price": float(r["monthly_price"]) if r["monthly_price"] is not None else None,
                "annual_price": float(r["annual_price"]) if r["annual_price"] is not None else None,
                "features": r["features"],
            },
            default=str,
            indent=2,
        )
    sql = "SELECT plan_id, plan_name, monthly_price, annual_price, features FROM retail.pricing_plans ORDER BY monthly_price NULLS LAST;"
    rows = await db_provider.execute_query(sql)
    if not rows:
        return "No pricing plans configured."
    out = [
        {
            "doc_id": f"plan-{r['plan_id']}",
            "plan_name": r["plan_name"],
            "monthly_price": float(r["monthly_price"]) if r["monthly_price"] is not None else None,
            "annual_price": float(r["annual_price"]) if r["annual_price"] is not None else None,
            "features": r["features"],
        }
        for r in rows
    ]
    return json.dumps(out, default=str, indent=2)


@mcp.tool(annotations={"title": "Compare Two Plans", "readOnlyHint": True, "openWorldHint": False})
async def compare_plans(
    plan_a: Annotated[str, Field(description="First plan name")],
    plan_b: Annotated[str, Field(description="Second plan name")],
    ctx: Context,
) -> str:
    """Side-by-side comparison of two pricing plans (price + features)."""
    if not db_provider:
        raise ToolError("Database not configured.")
    if not await db_provider.table_exists("retail", "pricing_plans"):
        return "Pricing plans are not provisioned yet."
    sql = "SELECT plan_id, plan_name, monthly_price, annual_price, features FROM retail.pricing_plans WHERE lower(plan_name) IN (lower($1), lower($2));"
    rows = await db_provider.execute_query(sql, plan_a, plan_b)
    if len(rows) < 2:
        names = [r["plan_name"] for r in rows]
        return f"Could not find both plans (found: {names})."
    return json.dumps(
        [
            {
                "doc_id": f"plan-{r['plan_id']}",
                "plan_name": r["plan_name"],
                "monthly_price": float(r["monthly_price"]) if r["monthly_price"] is not None else None,
                "annual_price": float(r["annual_price"]) if r["annual_price"] is not None else None,
                "features": r["features"],
            }
            for r in rows
        ],
        default=str,
        indent=2,
    )


# Streamable-HTTP ASGI app
app = mcp.http_app()


def run():
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    run()
