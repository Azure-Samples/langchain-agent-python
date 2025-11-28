#!/bin/bash
set -e

echo "Running post-provision setup..."

# Get environment values
echo "Retrieving environment values..."
POSTGRES_URL=$(azd env get-values --output json | jq -r '.POSTGRES_URL // empty')

# Populate database with sales data
if [ -n "$POSTGRES_URL" ]; then
  echo ""
  echo "📊 Populating database with sales data..."
  
  # Check if data files exist
  if [ ! -f "data/product_data.json" ] || [ ! -f "data/reference_data.json" ]; then
    echo "⚠️  Warning: Data files not found. Downloading..."
    echo ""
    echo "Downloading product_data.json (~2-3 GB, this may take a few minutes)..."
    curl -L --progress-bar \
      "https://raw.githubusercontent.com/microsoft/aitour26-WRK540-unlock-your-agents-potential-with-model-context-protocol/main/data/database/product_data.json" \
      -o data/product_data.json
    
    echo "Downloading reference_data.json..."
    curl -L --progress-bar \
      "https://raw.githubusercontent.com/microsoft/aitour26-WRK540-unlock-your-agents-potential-with-model-context-protocol/main/data/database/reference_data.json" \
      -o data/reference_data.json
  fi
  
  # Install dependencies if needed
  if ! python3 -c "import asyncpg" 2>/dev/null; then
    echo "Installing required Python packages..."
    pip install -q asyncpg
  fi
  
  # Run database generation script
  echo "Running database generation script..."
  export POSTGRES_URL="$POSTGRES_URL"
  python3 data/generate_database.py
  
  echo "✅ Database populated successfully!"
else
  echo "⚠️  POSTGRES_URL not found - skipping database population"
fi

echo ""
echo "✅ Post-provision setup complete!"
echo ""

# Get service URLs from azd environment
AGENT_URL=$(azd env get-values --output json | jq -r '.AGENT_URL // empty')
MCP_SERVER_URL=$(azd env get-values --output json | jq -r '.MCP_SERVER_URL // empty')

if [ -n "$AGENT_URL" ]; then
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "🚀 Your LangChain Agent is Ready!"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  echo "🌐 WEB CHAT INTERFACE (Open in browser):"
  echo "   ${AGENT_URL}/"
  echo ""
  echo "📊 API ENDPOINTS:"
  echo "   Chat API:      ${AGENT_URL}/api/chat (POST with JSON)"
  echo "   Health Check:  ${AGENT_URL}/api/health"
  if [ -n "$MCP_SERVER_URL" ]; then
    echo "   MCP Server:    ${MCP_SERVER_URL}/mcp"
  fi
  echo ""
  echo "💡 Try these questions in the web interface:"
  echo "   • What tables are in the database?"
  echo "   • How many products do we have?"
  echo "   • Show me the top 5 most expensive products"
  echo "   • Find hammers using semantic search"
  echo ""
  echo "🔧 Test via curl:"
  echo "   curl -X POST ${AGENT_URL}/api/chat \\"
  echo "     -H 'Content-Type: application/json' \\"
  echo "     -d '{\"message\":\"What tools do you have?\"}'"
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
fi
echo ""
