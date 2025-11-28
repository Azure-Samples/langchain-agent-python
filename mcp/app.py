"""
MCP Server for Zava Sales Analysis
Runs with uvicorn on Azure Container Apps
"""

import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import asyncpg
import uvicorn
from dotenv import load_dotenv

# Load environment variables from .env.local (for local development)
env_path = Path(__file__).parent.parent / ".env.local"
load_dotenv(env_path)

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from openai import AsyncAzureOpenAI
from pydantic import Field

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global providers (initialized in lifespan)
db_provider: Optional["PostgreSQLProvider"] = None
embedding_provider: Optional["SemanticSearchEmbedding"] = None


def parse_postgres_url(url: str) -> dict:
    """Parse PostgreSQL URL into connection parameters."""
    # Parse pattern: postgresql://user:password@host:port/database?params
    match = re.match(
        r"postgresql://([^:]+):([^@]+)@([^:]+):(\d+)/([^?]+)(\?(.+))?", url
    )
    if match:
        user, password, host, port, database, _, params = match.groups()
        result = {
            "user": user,
            "password": password,
            "host": host,
            "port": int(port),
            "database": database,
        }
        # Parse query parameters
        if params:
            for param in params.split("&"):
                if "=" in param:
                    key, value = param.split("=", 1)
                    if key == "sslmode":
                        result["ssl"] = value
        return result
    raise ValueError(f"Invalid PostgreSQL URL format: {url}")


class PostgreSQLProvider:
    """PostgreSQL database provider with pgvector support."""

    def __init__(self, connection_url: str):
        self.connection_params = parse_postgres_url(connection_url)
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        """Create connection pool."""
        try:
            self.pool = await asyncpg.create_pool(
                **self.connection_params, min_size=1, max_size=10
            )
            logger.info("✅ PostgreSQL connection pool established")
        except Exception as e:
            logger.error(f"❌ Failed to connect to PostgreSQL: {e}")
            raise

    async def close(self):
        """Close connection pool."""
        if self.pool:
            await self.pool.close()
            logger.info("PostgreSQL connection pool closed")

    async def execute_query(self, query: str) -> list[dict]:
        """Execute a SELECT query and return results."""
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query)
            return [dict(row) for row in rows]

    async def get_table_schemas(self) -> str:
        """Get schema information for all tables in the retail schema."""
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

            # Group by table
            tables = {}
            for row in rows:
                table_name = row["table_name"]
                if table_name not in tables:
                    tables[table_name] = []

                tables[table_name].append(
                    {
                        "column": row["column_name"],
                        "type": row["data_type"],
                        "nullable": row["is_nullable"] == "YES",
                        "default": row["column_default"],
                    }
                )

            return json.dumps(tables, indent=2)


class SemanticSearchEmbedding:
    """Semantic search using Azure OpenAI embeddings and pgvector."""

    def __init__(self, openai_endpoint: str, embedding_deployment: str):
        self.openai_endpoint = openai_endpoint
        self.embedding_deployment = embedding_deployment

        # Initialize Azure OpenAI async client with Entra ID auth
        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential, "https://cognitiveservices.azure.com/.default"
        )

        self.client = AsyncAzureOpenAI(
            api_version="2024-10-21",
            azure_endpoint=openai_endpoint,
            azure_ad_token_provider=token_provider,
        )
        logger.info(f"✅ Azure OpenAI async client initialized: {openai_endpoint}")

    async def get_embedding(self, text: str) -> list[float]:
        """Get embedding vector for text asynchronously."""
        response = await self.client.embeddings.create(
            input=text, model=self.embedding_deployment
        )
        return response.data[0].embedding

    async def search_products(
        self,
        query: str,
        max_rows: int = 5,
        threshold: float = 0.7,
        db_pool: asyncpg.Pool = None,
        ctx: Context = None,
    ) -> str:
        """Search for products using semantic similarity."""
        if not db_pool:
            raise ToolError("Database not connected")

        # Report progress: Getting embedding
        if ctx:
            await ctx.report_progress(progress=1, total=3)
            await ctx.info(f"Getting embedding for query: {query[:50]}...")

        # Get embedding for query (1536-dim from text-embedding-3-small)
        query_embedding = await self.get_embedding(query)

        # Convert embedding list to pgvector string format
        embedding_str = "[" + ",".join(map(str, query_embedding)) + "]"

        # Report progress: Searching database
        if ctx:
            await ctx.report_progress(progress=2, total=3)
            await ctx.info("Searching products in database...")

        # Search using pgvector cosine similarity on description embeddings
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

            # Report progress: Done
            if ctx:
                await ctx.report_progress(progress=3, total=3)

            if not rows:
                return f"No products found matching '{query}' with similarity > {threshold}"

            results = []
            for row in rows:
                results.append(
                    f"• {row['product_name']} ({row['category_name']}) - "
                    f"${row['base_price']:.2f} - Similarity: {row['similarity']:.2%}\n"
                    f"  {row['product_description'][:100]}..."
                )

            return "\n\n".join(results)


@asynccontextmanager
async def lifespan(mcp_server: FastMCP):
    """Lifespan context manager for proper initialization and cleanup."""
    global db_provider, embedding_provider

    logger.info("🚀 Starting MCP server initialization...")

    # Initialize PostgreSQL provider
    postgres_url = os.getenv("POSTGRES_URL")
    if postgres_url:
        db_provider = PostgreSQLProvider(postgres_url)
        await db_provider.connect()
        logger.info("✅ Database provider connected")
    else:
        logger.warning("⚠️  POSTGRES_URL not set - database tools will not work")
        db_provider = None

    # Initialize embedding provider
    openai_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    embedding_deployment = os.getenv(
        "AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small"
    )

    if openai_endpoint:
        try:
            embedding_provider = SemanticSearchEmbedding(
                openai_endpoint, embedding_deployment
            )
            logger.info("✅ Embedding provider initialized")
        except Exception as e:
            logger.error(f"Failed to initialize embeddings: {e}")
            embedding_provider = None
    else:
        logger.warning(
            "⚠️  AZURE_OPENAI_ENDPOINT not set - semantic search will not work"
        )
        embedding_provider = None

    logger.info("✅ MCP server ready")

    yield  # Server is running

    # Cleanup on shutdown
    logger.info("🛑 Shutting down MCP server...")
    if db_provider:
        await db_provider.close()
    logger.info("✅ MCP server shutdown complete")


# Create MCP server instance with lifespan
mcp = FastMCP("Zava Sales Analysis Tools", lifespan=lifespan)


def validate_sql_query(query: str) -> None:
    """Validate SQL query for safety.

    Raises:
        ToolError: If the query is not safe to execute
    """
    # Strip whitespace and trailing semicolon (trailing semicolon is safe)
    normalized = query.strip()
    if normalized.endswith(";"):
        normalized = normalized[:-1].strip()

    normalized_upper = normalized.upper()

    # Must start with SELECT
    if not normalized_upper.startswith("SELECT"):
        raise ToolError("Only SELECT queries are allowed")

    # Check for multiple statements (semicolon in the middle of query)
    if ";" in normalized:
        raise ToolError("Multiple SQL statements are not allowed")

    # Check for dangerous patterns (SQL injection prevention)
    dangerous_patterns = [
        "--",  # SQL comments
        "/*",  # Block comments
        "DROP ",
        "DELETE ",
        "INSERT ",
        "UPDATE ",
        "ALTER ",
        "CREATE ",
        "TRUNCATE ",
        "GRANT ",
        "REVOKE ",
        "EXEC ",
        "EXECUTE ",
    ]

    for pattern in dangerous_patterns:
        if pattern in normalized_upper:
            raise ToolError(f"Query contains forbidden pattern: {pattern.strip()}")


# MCP Tools


@mcp.tool(
    annotations={
        "title": "Get Current UTC Date",
        "readOnlyHint": True,
        "openWorldHint": False,
    }
)
def get_current_utc_date() -> str:
    """Get the current UTC date and time.

    Returns:
        Current UTC timestamp in ISO format
    """
    return datetime.now(timezone.utc).isoformat()


@mcp.tool(
    annotations={
        "title": "Get Database Table Schemas",
        "readOnlyHint": True,
        "openWorldHint": False,
    }
)
async def get_table_schemas(ctx: Context) -> str:
    """Get the schema information for all database tables.

    Returns:
        JSON string containing table schemas with columns, types, and constraints
    """
    if not db_provider:
        raise ToolError(
            "Database not configured. Set POSTGRES_URL environment variable."
        )

    try:
        await ctx.info("Fetching database table schemas...")
        return await db_provider.get_table_schemas()
    except Exception as e:
        await ctx.error(f"Error getting schemas: {e}")
        raise ToolError(f"Failed to get table schemas: {str(e)}")


@mcp.tool(
    annotations={
        "title": "Execute Sales Query",
        "readOnlyHint": True,
        "openWorldHint": False,
    }
)
async def execute_sales_query(
    query: Annotated[
        str,
        Field(
            description="SQL query to execute against the sales database. All tables are in the 'retail' schema."
        ),
    ],
    ctx: Context,
) -> str:
    """Execute a SQL query against the sales database.

    Args:
        query: SQL query to execute (SELECT statements only). All tables are in the 'retail' schema.

    Returns:
        JSON string containing query results
    """
    if not db_provider:
        raise ToolError(
            "Database not configured. Set POSTGRES_URL environment variable."
        )

    # Validate query for security
    validate_sql_query(query)

    try:
        await ctx.info(f"Executing query: {query[:100]}...")
        results = await db_provider.execute_query(query)
        await ctx.info(f"Query returned {len(results)} rows")
        return json.dumps(results, indent=2, default=str)
    except Exception as e:
        await ctx.error(f"Error executing query: {e}")
        raise ToolError(f"Query execution failed: {str(e)}")


@mcp.tool(
    annotations={
        "title": "Semantic Product Search",
        "readOnlyHint": True,
        "openWorldHint": True,  # Calls Azure OpenAI API
    }
)
async def semantic_search_products(
    query: Annotated[str, Field(description="Search query to find relevant products")],
    ctx: Context,
    max_rows: Annotated[
        int, Field(description="Maximum number of results to return", ge=1, le=20)
    ] = 5,
    threshold: Annotated[
        float, Field(description="Similarity threshold (0-1)", ge=0, le=1)
    ] = 0.7,
) -> str:
    """Search for products using semantic similarity with pgvector.

    Args:
        query: Natural language search query
        max_rows: Maximum number of results (1-20)
        threshold: Minimum similarity score (0-1)

    Returns:
        Formatted list of matching products with similarity scores
    """
    if not embedding_provider:
        raise ToolError(
            "Semantic search not configured. Set AZURE_OPENAI_ENDPOINT environment variable."
        )

    if not db_provider or not db_provider.pool:
        raise ToolError(
            "Database not connected. Set POSTGRES_URL environment variable."
        )

    try:
        return await embedding_provider.search_products(
            query, max_rows, threshold, db_provider.pool, ctx
        )
    except ToolError:
        raise
    except Exception as e:
        await ctx.error(f"Error in semantic search: {e}")
        raise ToolError(f"Semantic search failed: {str(e)}")


# Create the Starlette app (using http_app instead of deprecated streamable_http_app)
app = mcp.http_app()


def run():
    """Run the server with uvicorn."""
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    run()
