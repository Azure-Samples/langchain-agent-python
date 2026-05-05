"""MCP Server for Zava Sales Analysis.

Exposes 4 read-only MCP tools (current UTC time, table schemas, SQL query,
semantic product search) over streamable HTTP. Connects to Postgres
(via asyncpg DSN) and Azure OpenAI embeddings (Entra ID auth).
"""

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import re
from urllib.parse import quote

import asyncpg
import uvicorn
from dotenv import load_dotenv

# Load environment variables from .env.local (for local development)
load_dotenv(Path(__file__).parent.parent / ".env.local")

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from openai import AsyncAzureOpenAI
from pydantic import Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Globals populated in lifespan. Kept module-level for compatibility with
# FastMCP's @tool decorator pattern (handlers are module functions).
db_provider: Optional["PostgreSQLProvider"] = None
embedding_provider: Optional["SemanticSearchEmbedding"] = None


_DSN_RE = re.compile(r"^([^:]+://)([^:/@]+):([^@]+)@(.+)$")


def _safe_dsn(url: str) -> str:
    """Re-encode userinfo so asyncpg can parse DSNs with special chars in passwords.

    `azd` provisions Postgres with a generated password that may contain `#`,
    `@`, `/`, etc. urlparse treats `#` as a fragment delimiter, so we extract
    user/password with a tolerant regex, then percent-encode each.
    """
    m = _DSN_RE.match(url)
    if not m:
        return url
    scheme, user, password, rest = m.groups()
    return f"{scheme}{quote(user, safe='')}:{quote(password, safe='')}@{rest}"


class PostgreSQLProvider:
    """PostgreSQL connection pool wrapper with pgvector support."""

    def __init__(self, dsn: str):
        # asyncpg accepts a DSN string directly; we just URL-encode userinfo
        # to handle generated passwords with `#`, `@`, etc.
        self.dsn = _safe_dsn(dsn)
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        try:
            self.pool = await asyncpg.create_pool(dsn=self.dsn, min_size=1, max_size=10)
            logger.info("✅ PostgreSQL connection pool established")
        except Exception as e:
            logger.error(f"❌ Failed to connect to PostgreSQL: {e}")
            raise

    async def close(self):
        if self.pool:
            await self.pool.close()
            logger.info("PostgreSQL connection pool closed")

    async def execute_query(self, query: str) -> list[dict]:
        if not self.pool:
            await self.connect()
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query)
            return [dict(row) for row in rows]

    async def get_table_schemas(self) -> str:
        if not self.pool:
            await self.connect()

        schema_query = """
        SELECT
            table_name,
            column_name,
            data_type,
            is_nullable,
            column_default
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


class SemanticSearchEmbedding:
    """Semantic search using Azure OpenAI embeddings + pgvector cosine."""

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
        logger.info(
            "✅ Azure OpenAI client initialized (endpoint=%s, deployment=%s, api_version=%s)",
            openai_endpoint, embedding_deployment, api_version,
        )

    async def get_embedding(self, text: str) -> list[float]:
        response = await self.client.embeddings.create(
            input=text, model=self.embedding_deployment
        )
        return response.data[0].embedding

    async def search_products(
        self,
        query: str,
        max_rows: int = 5,
        threshold: float = 0.7,
        db_pool: Optional[asyncpg.Pool] = None,
        ctx: Optional[Context] = None,
    ) -> str:
        if not db_pool:
            raise ToolError("Database not connected")

        if ctx:
            await ctx.report_progress(progress=1, total=3)
            await ctx.info(f"Getting embedding for query: {query[:50]}...")

        query_embedding = await self.get_embedding(query)
        embedding_str = "[" + ",".join(map(str, query_embedding)) + "]"

        if ctx:
            await ctx.report_progress(progress=2, total=3)
            await ctx.info("Searching products in database...")

        search_query = """
        SELECT
            p.product_name,
            p.product_description,
            c.category_name,
            p.base_price,
            1 - (de.description_embedding <=> $1::vector) as similarity
        FROM retail.products p
        JOIN retail.categories c ON p.category_id = c.category_id
        JOIN retail.product_description_embeddings de ON p.product_id = de.product_id
        WHERE 1 - (de.description_embedding <=> $1::vector) > $2
        ORDER BY similarity DESC
        LIMIT $3;
        """

        async with db_pool.acquire() as conn:
            rows = await conn.fetch(search_query, embedding_str, threshold, max_rows)
            if ctx:
                await ctx.report_progress(progress=3, total=3)

            if not rows:
                return f"No products found matching '{query}' with similarity > {threshold}"

            return "\n\n".join(
                f"• {row['product_name']} ({row['category_name']}) - "
                f"${row['base_price']:.2f} - Similarity: {row['similarity']:.2%}\n"
                f"  {row['product_description'][:100]}..."
                for row in rows
            )


@asynccontextmanager
async def lifespan(mcp_server: FastMCP):
    """Initialise DB + embedding providers; tear down on shutdown."""
    global db_provider, embedding_provider

    logger.info("🚀 Starting MCP server initialization...")

    postgres_url = os.getenv("POSTGRES_URL")
    if postgres_url:
        db_provider = PostgreSQLProvider(postgres_url)
        await db_provider.connect()
    else:
        logger.warning("⚠️  POSTGRES_URL not set - database tools will not work")

    openai_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    embedding_deployment = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")

    if openai_endpoint:
        try:
            embedding_provider = SemanticSearchEmbedding(
                openai_endpoint, embedding_deployment, api_version
            )
        except Exception as e:
            logger.error(f"Failed to initialize embeddings: {e}")
    else:
        logger.warning("⚠️  AZURE_OPENAI_ENDPOINT not set - semantic search will not work")

    logger.info("✅ MCP server ready")
    try:
        yield
    finally:
        logger.info("🛑 Shutting down MCP server...")
        if db_provider:
            await db_provider.close()


mcp = FastMCP("Zava Sales Analysis Tools", lifespan=lifespan)


# Defence-in-depth SQL filter. The proper hardening is a read-only Postgres
# role (no INSERT/UPDATE/DELETE/DDL grants) configured in Bicep. This filter
# remains a belt-and-braces guard: a deny-list will not catch every clever
# bypass on its own, but combined with a read-only role it adds friction
# against accidental destructive queries.
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


# ---- MCP Tools -------------------------------------------------------------
@mcp.tool(annotations={"title": "Get Current UTC Date", "readOnlyHint": True, "openWorldHint": False})
def get_current_utc_date() -> str:
    """Return the current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


@mcp.tool(annotations={"title": "Get Database Table Schemas", "readOnlyHint": True, "openWorldHint": False})
async def get_table_schemas(ctx: Context) -> str:
    """Return JSON describing the columns of every table in the `retail` schema."""
    if not db_provider:
        raise ToolError("Database not configured. Set POSTGRES_URL environment variable.")
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
    """Execute a read-only SQL query and return the results as JSON."""
    if not db_provider:
        raise ToolError("Database not configured. Set POSTGRES_URL environment variable.")

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
    query: Annotated[str, Field(description="Search query to find relevant products")],
    ctx: Context,
    max_rows: Annotated[int, Field(description="Maximum results", ge=1, le=20)] = 5,
    threshold: Annotated[float, Field(description="Similarity threshold (0-1)", ge=0, le=1)] = 0.7,
) -> str:
    """Find products by semantic similarity using pgvector cosine distance."""
    if not embedding_provider:
        raise ToolError("Semantic search not configured. Set AZURE_OPENAI_ENDPOINT.")
    if not db_provider or not db_provider.pool:
        raise ToolError("Database not connected. Set POSTGRES_URL.")

    try:
        return await embedding_provider.search_products(
            query, max_rows, threshold, db_provider.pool, ctx
        )
    except ToolError:
        raise
    except Exception as e:
        await ctx.error(f"Error in semantic search: {e}")
        raise ToolError(f"Semantic search failed: {e}")


# Streamable-HTTP ASGI app
app = mcp.http_app()


def run():
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    run()

