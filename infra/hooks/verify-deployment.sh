#!/bin/bash
# Deployment Verification Script
# This script verifies that all components are properly deployed and configured

set -e

echo "🔍 Verifying deployment configuration..."
echo ""

# Check required environment variables
echo "📋 Checking environment variables..."
MISSING_VARS=()

check_var() {
  VAR_NAME=$1
  VAR_VALUE=$(azd env get-values --output json | jq -r ".${VAR_NAME} // empty")
  if [ -z "$VAR_VALUE" ]; then
    echo "  ❌ $VAR_NAME: NOT SET"
    MISSING_VARS+=("$VAR_NAME")
  else
    echo "  ✅ $VAR_NAME: ${VAR_VALUE:0:50}..."
  fi
}

check_var "POSTGRES_URL"
check_var "POSTGRES_HOST"
check_var "POSTGRES_DATABASE"
check_var "MCP_SERVER_URL"
check_var "AGENT_URL"
check_var "AZURE_OPENAI_ENDPOINT"
check_var "AZURE_OPENAI_DEPLOYMENT"
check_var "AZURE_OPENAI_EMBEDDING_DEPLOYMENT"

if [ ${#MISSING_VARS[@]} -gt 0 ]; then
  echo ""
  echo "⚠️  Missing required environment variables: ${MISSING_VARS[*]}"
  echo "   Run 'azd provision' to set up infrastructure"
  exit 1
fi

echo ""
echo "📊 Checking database population..."
POSTGRES_URL=$(azd env get-values --output json | jq -r '.POSTGRES_URL')

# Check if Python and asyncpg are available
if ! command -v python3 &> /dev/null; then
  echo "  ⚠️  Python3 not found - cannot verify database"
else
  if ! python3 -c "import asyncpg" 2>/dev/null; then
    echo "  ⚠️  asyncpg not installed - installing..."
    pip install -q asyncpg
  fi
  
  # Check database tables
  python3 << PYEOF
import asyncio
import asyncpg
import sys
import re

def parse_postgres_url(url: str) -> dict:
    match = re.match(r'postgresql://([^:]+):([^@]+)@([^:]+):(\d+)/([^?]+)(\?(.+))?', url)
    if match:
        user, password, host, port, database, _, params = match.groups()
        result = {'user': user, 'password': password, 'host': host, 'port': int(port), 'database': database}
        if params:
            for param in params.split('&'):
                if '=' in param:
                    key, value = param.split('=', 1)
                    if key == 'sslmode':
                        result['ssl'] = value
        return result
    return {}

async def check_db():
    try:
        params = parse_postgres_url('$POSTGRES_URL')
        conn = await asyncpg.connect(**params)
        
        # Check key tables
        tables = ['products', 'categories', 'stores', 'product_description_embeddings']
        counts = {}
        
        for table in tables:
            try:
                count = await conn.fetchval(f'SELECT COUNT(*) FROM retail.{table}')
                counts[table] = count
            except Exception as e:
                counts[table] = f"Error: {e}"
        
        await conn.close()
        
        # Display results
        print("  Database tables:")
        for table, count in counts.items():
            if isinstance(count, int):
                status = "✅" if count > 0 else "⚠️ "
                print(f"    {status} {table}: {count} rows")
            else:
                print(f"    ❌ {table}: {count}")
        
        # Check if database is populated
        if all(isinstance(c, int) and c > 0 for c in counts.values()):
            print("  ✅ Database fully populated")
            return 0
        else:
            print("  ⚠️  Database not fully populated - run postprovision script")
            return 1
            
    except Exception as e:
        print(f"  ❌ Database connection failed: {e}")
        return 1

sys.exit(asyncio.run(check_db()))
PYEOF
  
  DB_STATUS=$?
fi

echo ""
echo "🌐 Checking service endpoints..."

MCP_URL=$(azd env get-values --output json | jq -r '.MCP_SERVER_URL')
AGENT_URL=$(azd env get-values --output json | jq -r '.AGENT_URL')

# Test MCP Server health
echo "  Testing MCP Server..."
MCP_RESPONSE=$(curl -s -w "%{http_code}" -o /dev/null "${MCP_URL}" 2>/dev/null || echo "000")
if [ "$MCP_RESPONSE" = "404" ] || [ "$MCP_RESPONSE" = "200" ]; then
  echo "    ✅ MCP Server responding (HTTP $MCP_RESPONSE)"
else
  echo "    ⚠️  MCP Server not responding properly (HTTP $MCP_RESPONSE)"
fi

# Test Agent health
echo "  Testing Agent..."
AGENT_HEALTH=$(curl -s "${AGENT_URL}/api/health" 2>/dev/null | jq -r '.status // empty' || echo "")
if [ "$AGENT_HEALTH" = "healthy" ]; then
  echo "    ✅ Agent healthy"
else
  echo "    ⚠️  Agent health check failed"
fi

echo ""
echo "📝 Summary:"
echo ""
echo "  MCP Server:  ${MCP_URL}/mcp"
echo "  Agent API:   ${AGENT_URL}/api/chat"
echo "  Agent Health: ${AGENT_URL}/api/health"
echo ""
echo "💡 Test commands:"
echo ""
echo "  # Test agent chat"
echo "  curl -X POST ${AGENT_URL}/api/chat \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"message\":\"What tables are in the database?\"}'"
echo ""
echo "  # Test agent health"
echo "  curl ${AGENT_URL}/api/health"
echo ""

if [ ${#MISSING_VARS[@]} -eq 0 ] && [ "$DB_STATUS" = "0" ]; then
  echo "✅ All checks passed! Deployment is ready."
  exit 0
else
  echo "⚠️  Some checks failed. Review the output above."
  exit 1
fi
