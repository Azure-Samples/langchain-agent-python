# Local Development Guide

This guide explains how to run the entire solution locally, including PostgreSQL with pgvector for semantic search.

## Quick Start (5 minutes)

### Option 1: Use Azure PostgreSQL (Recommended)

Deploy to Azure first, then use the cloud database locally:

```bash
# 1. Deploy to Azure
azd up

# 2. Get database connection string
azd env get-values > .env.local

# 3. Add local MCP server URL
echo "MCP_SERVER_URL=http://localhost:8000" >> .env.local

# 4. Run locally (see below)
```

### Option 2: Local PostgreSQL with Docker Compose

Run everything locally including the database:

```bash
# 1. Start PostgreSQL with pgvector
docker-compose up -d

# 2. Configure environment
cp .env.example .env.local
# Edit .env.local with your Azure OpenAI credentials

# 3. Initialize database
cd data
source ../.env.local
python generate_database.py

# 4. Regenerate embeddings (IMPORTANT - see note below)
python regenerate_embeddings.py

# 5. Run MCP server and agent (see below)
```

> **⚠️ Important:** The pre-generated embeddings in `products_pregenerated.json` were created with a specific embedding model. If your Azure OpenAI deployment uses a different embedding model (e.g., `text-embedding-ada-002` vs `text-embedding-ada-002`), you MUST run `regenerate_embeddings.py` after initializing the database. Otherwise, semantic product search will not work correctly.

## Prerequisites

- **Python 3.11+** - [Download](https://www.python.org/downloads/)
- **Docker Desktop** (for local PostgreSQL) - [Install](https://www.docker.com/products/docker-desktop)
- **Azure CLI** - [Install](https://learn.microsoft.com/cli/azure/install-azure-cli)
- **Azure OpenAI Access** - You'll use a cloud Azure OpenAI instance even for local dev

## Setup Instructions

### 1. Configure Environment

```bash
# Copy environment template
cp .env.example .env.local

# Edit .env.local with your values
# Get these from: azd env get-values (after deploying to Azure)
```

**Required environment variables:**

```bash
# Azure OpenAI (required - uses cloud instance)
AZURE_OPENAI_ENDPOINT=https://your-openai.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini
AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-ada-002  # or text-embedding-ada-002
AZURE_TENANT_ID=your-tenant-id

# MCP Server (local)
MCP_SERVER_URL=http://localhost:8000

# PostgreSQL (choose one option below)
# Option A: Local Docker
POSTGRES_URL=postgresql://postgres:postgres@localhost:5432/zava

# Option B: Azure PostgreSQL (from azd deployment)
POSTGRES_URL=postgresql://pgadmin:password@psql-xxx.postgres.database.azure.com:5432/zava?sslmode=require
```

> **Note:** The `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` must match the embedding model deployed in your Azure OpenAI resource. Common values are `text-embedding-ada-002` or `text-embedding-ada-002`. After changing this, run `data/regenerate_embeddings.py` to update the database.

### 2. Start Local PostgreSQL (Option 2)

Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  postgres:
    image: pgvector/pgvector:pg17
    container_name: zava-postgres
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: zava
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 10s
      timeout: 5s
      retries: 5

volumes:
  postgres_data:
```

Start the database:

```bash
# Start PostgreSQL
docker-compose up -d

# Wait for it to be ready
docker-compose logs -f postgres

# Verify it's running
docker ps | grep zava-postgres
```

### 3. Initialize Database

The repository includes pre-generated data files, so no download is needed:

```bash
cd data

# Set database URL
export POSTGRES_URL='postgresql://postgres:postgres@localhost:5432/zava'

# Generate database schema and load pre-generated data
python generate_database.py
```

**Pre-generated data included:**
- `products_pregenerated.json` - 424 products with pre-computed embeddings
- `customers_pregenerated.json` - 500 sample customers  
- `orders_pregenerated.json` - 2000 sample orders

**Expected output:**
```
✅ Connected to PostgreSQL
✅ Created schema 'retail'
✅ Enabled pgvector extension
✅ Loading products from pre-generated data...
✅ Categories and types loaded
✅ Loaded 424 products with embeddings from JSON
✅ Created 8 stores
✅ Loaded 500 customers from JSON
✅ Loaded 2000 orders with items from JSON
✅ Generated inventory records
✅ Indexes created successfully
✅ Database generation completed successfully!
```

### 4. Install Dependencies

```bash
# MCP Server dependencies
cd mcp
pip install -r requirements.txt

# Agent dependencies
cd ../agent
pip install -r requirements.txt
```

### 5. Run MCP Server

**Terminal 1:**

```bash
cd mcp
source ../.env.local  # Load environment variables
python app.py
```

**Expected output:**
```
🚀 Starting MCP server initialization...
✅ PostgreSQL connection pool established
✅ Database provider connected
✅ Azure OpenAI async client initialized
✅ Embedding provider initialized
✅ MCP server ready
INFO: Uvicorn running on http://0.0.0.0:8000
```

**Test the server:**

```bash
# Check MCP endpoint
curl http://localhost:8000/mcp/
```

### 6. Run Agent

**Terminal 2:**

```bash
cd agent
source ../.env.local  # Load environment variables
PORT=8001 python app.py
```

**Expected output:**
```
INFO:__main__:🔧 Running in LOCAL mode - using langchain-mcp-adapters for MCP tools
INFO:     Started server process [xxxxx]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8001
```

**Open browser to:** http://localhost:8001

**Test queries:**

```bash
# Example 1: Semantic search
"Show me products for outdoor electrical work"

# Example 2: Sales analysis
"What were our top selling categories last quarter?"

# Example 3: Time-based queries
"Show me orders from today"

# Example 4: Inventory queries
"Which products are low in stock at Store 1?"
```

## Local Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    Your Local Machine                        │
│                                                              │
│  ┌─────────────────┐      ┌──────────────────┐             │
│  │  Agent          │──────│  MCP Server      │             │
│  │  agent/app.py   │ HTTP │  mcp/app.py      │             │
│  │  Port: 8001     │◄─────│  Port: 8000      │             │
│  └────────┬────────┘      └────────┬─────────┘             │
│           │                         │                        │
│           │ Entra ID               │                        │
│           ▼                         ▼                        │
│  ┌─────────────────┐      ┌──────────────────┐             │
│  │   Azure Cloud   │      │  Docker (Local)  │             │
│  │                 │      │                  │             │
│  │  Azure OpenAI   │      │  PostgreSQL 17   │             │
│  │  - GPT-4o-mini  │      │  - pgvector      │             │
│  │  - Embeddings   │      │  - Port 5432     │             │
│  └─────────────────┘      └──────────────────┘             │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

## Troubleshooting

### PostgreSQL Issues

**❌ "Connection refused" on port 5432**
```bash
# Check if PostgreSQL is running
docker ps | grep postgres

# If not, start it
docker-compose up -d

# Check logs
docker-compose logs postgres
```

**❌ "Database 'zava' does not exist"**
```bash
# The generate_database.py script creates the database
cd data
export POSTGRES_URL='postgresql://postgres:postgres@localhost:5432/zava'
python generate_database.py
```

**❌ "Extension 'vector' does not exist"**
```bash
# Verify you're using pgvector/pgvector:pg17 image
docker-compose down
docker-compose up -d

# The generate_database.py script enables the extension
```

### MCP Server Issues

**❌ "POSTGRES_URL not set - database tools will not work"**
```bash
# Load environment variables before running
source .env.local
python app.py
```

**❌ "Failed to initialize embeddings"**
```bash
# Verify Azure OpenAI credentials
az login
az account show

# Test token retrieval
python -c "from azure.identity import DefaultAzureCredential; print(DefaultAzureCredential().get_token('https://cognitiveservices.azure.com/.default').token[:20])"
```

### Agent Issues

**❌ "MCP server not accessible"**
```bash
# Ensure MCP server is running (Terminal 1)
curl http://localhost:8000/mcp

# Verify MCP_SERVER_URL in .env.local
cat .env.local | grep MCP_SERVER_URL
```

**❌ "Azure OpenAI authentication failed"**
```bash
# Login to Azure
az login

# Verify tenant ID
az account show --query tenantId -o tsv

# Update AZURE_TENANT_ID in .env.local
```

### Semantic Search Issues

**❌ "Semantic search returns no results" or "Product search not finding expected products"**

This usually means the embeddings in the database were generated with a different model than what's being used for queries.

```bash
# Regenerate embeddings with your current Azure OpenAI embedding model
cd data
source ../.env.local
python regenerate_embeddings.py
```

The script will:
1. Connect to your Azure OpenAI embedding deployment
2. Regenerate embeddings for all 424 products
3. Update the database
4. Verify the fix by testing a search for "hammers"

**Expected output after regeneration:**
```
✅ Successfully regenerated 424 embeddings!
Top 10 products by similarity to 'hammers':
  0.8475 - Finishing Hammer 13oz
  0.8454 - Ball Peen Hammer 12oz
  0.8420 - Professional Claw Hammer 16oz
  ...
```

## Database Management

### Connect to Local PostgreSQL

```bash
# Using Docker exec
docker exec -it zava-postgres psql -U postgres -d zava

# Using psql client (if installed)
psql postgresql://postgres:postgres@localhost:5432/zava
```

### Useful SQL Queries

```sql
-- List all tables
\dt retail.*

-- Count products
SELECT COUNT(*) FROM retail.products;

-- Check vector embeddings
SELECT COUNT(*) FROM retail.product_description_embeddings;

-- Test semantic search
SELECT 
    p.product_name,
    (pde.description_embedding <=> '[0.1, 0.2, ...]'::vector) as distance
FROM retail.product_description_embeddings pde
JOIN retail.products p ON pde.product_id = p.product_id
ORDER BY distance
LIMIT 5;

-- View schema
\d+ retail.products
```

### Reset Database

```bash
# Stop and remove container with data
docker-compose down -v

# Start fresh
docker-compose up -d

# Regenerate data
cd data
export POSTGRES_URL='postgresql://postgres:postgres@localhost:5432/zava'
python generate_database.py
```

## Performance Tips

### PostgreSQL

```sql
-- Create additional indexes for common queries
CREATE INDEX idx_products_category ON retail.products(category_id);
CREATE INDEX idx_orders_date ON retail.orders(order_date);

-- Analyze tables for query optimization
ANALYZE retail.products;
ANALYZE retail.orders;
```

### MCP Server

- **Connection pooling**: Already configured (1-10 connections)
- **Vector search**: Uses IVFFlat indexes for fast similarity search
- **Query limits**: Default LIMIT 20 to prevent large result sets

### Agent

- **Streaming**: Enabled by default for faster responses
- **Tool caching**: MCP tools are cached to reduce network calls

## VS Code Integration

### Pre-configured Tasks

The project includes pre-configured VS Code tasks in `.vscode/tasks.json`. Press `Cmd+Shift+P` (Mac) or `Ctrl+Shift+P` (Windows/Linux), then select "Tasks: Run Task" to see:

| Task | Description |
|------|-------------|
| **Start MCP Server** | Runs `python mcp/app.py` on port 8000 |
| **Start Agent** | Runs `python agent/app.py` on port 8001 |
| **Start PostgreSQL (Docker)** | Runs `docker-compose up -d` |
| **Stop PostgreSQL (Docker)** | Runs `docker-compose down` |
| **Initialize Database** | Runs `python data/generate_database.py` |
| **Azure Login** | Runs `az login && azd auth login` |
| **Deploy to Azure** | Runs `azd up` |

### Debugging

Create `.vscode/launch.json` for debugging:

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "MCP Server",
      "type": "debugpy",
      "request": "launch",
      "program": "${workspaceFolder}/mcp/app.py",
      "console": "integratedTerminal",
      "envFile": "${workspaceFolder}/.env.local"
    },
    {
      "name": "Agent",
      "type": "debugpy",
      "request": "launch",
      "program": "${workspaceFolder}/agent/app.py",
      "console": "integratedTerminal",
      "envFile": "${workspaceFolder}/.env.local",
      "env": {
        "PORT": "8001"
      }
    }
  ]
}
```

## Testing

### Test MCP Tools Directly

```bash
cd mcp
source ../.env.local

# Test database connection
python -c "
import asyncio
from mcp_server import PostgreSQLProvider
import os

async def test():
    db = PostgreSQLProvider(os.getenv('POSTGRES_URL'))
    await db.connect()
    schemas = await db.get_table_schemas()
    print(f'Found {len(schemas)} tables')
    await db.disconnect()

asyncio.run(test())
"

# Test semantic search
python -c "
import asyncio
from mcp_server import SemanticSearchEmbedding
import os

async def test():
    search = SemanticSearchEmbedding(
        os.getenv('AZURE_OPENAI_ENDPOINT'),
        os.getenv('AZURE_OPENAI_EMBEDDING_DEPLOYMENT')
    )
    result = await search.search_products('outdoor electrical box', max_results=3)
    print(f'Found {len(result[\"products\"])} products')

asyncio.run(test())
"
```

### Test Agent Queries

```bash
cd agent
source ../.env.local
python agent.py <<EOF
What were the top 5 selling products last month?
EOF
```

## Next Steps

Once local development is working:

1. **Make Changes** - Modify agent instructions, add MCP tools, update queries
2. **Test Locally** - Verify everything works with local database
3. **Deploy to Azure** - Run `azd up` to deploy changes
4. **Monitor** - Use `azd monitor` to view logs and metrics

## Differences: Local vs Production

| Aspect | Local | Production |
|--------|-------|------------|
| **PostgreSQL** | Docker (localhost:5432) | Azure Flexible Server |
| **Azure OpenAI** | Cloud (with Entra ID via Azure CLI) | Cloud (with Managed Identity) |
| **MCP Server** | Python process (port 8000) | Container App |
| **Agent** | Python process (port 8001) | Container App |
| **MCP Integration** | langchain-mcp-adapters | Azure Functions MCP extension |
| **Networking** | localhost | Azure Virtual Network |
| **SSL** | Optional | Required (sslmode=require) |
| **Monitoring** | Console logs | Application Insights |
| **Authentication** | Azure CLI login | Managed Identity |
| **Embeddings** | May need regeneration | Pre-configured |

## Resources

- [PostgreSQL Documentation](https://www.postgresql.org/docs/)
- [pgvector Documentation](https://github.com/pgvector/pgvector)
- [Docker Compose Documentation](https://docs.docker.com/compose/)
- [Azure Identity for Python](https://learn.microsoft.com/python/api/overview/azure/identity-readme)

---

**Need help?** Open an issue on [GitHub](https://github.com/Azure-Samples/langchain-agent-python/issues)
