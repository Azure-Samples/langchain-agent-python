#!/bin/bash

# Exit on any error
set -e

echo "🚀 Setting up development environment..."

# Upgrade pip
echo "📦 Upgrading pip..."
python -m pip install --upgrade pip

# Install Python development tools
echo "🔧 Installing Python development tools..."
pip install --user black pylint pytest ipykernel

# Install agent dependencies
echo "📦 Installing agent dependencies..."
cd agent && pip install -r requirements.txt && cd ..

# Install MCP server dependencies
echo "📦 Installing MCP server dependencies..."
cd mcp && pip install -r requirements.txt && cd ..

# Install data generation dependencies
echo "📦 Installing data generation dependencies..."
pip install asyncpg pgvector

# Create .env.local from example if it doesn't exist
if [ ! -f .env.local ]; then
    echo "📝 Creating .env.local from template..."
    cp .env.example .env.local
    echo "✅ Created .env.local - please update with your Azure credentials"
fi

# Verify installations
echo ""
echo "✅ Development environment ready!"
echo ""
echo "Installed tools:"
echo "  Python: $(python --version)"
echo "  Azure CLI: $(az version -o tsv | head -n 1)"
echo "  Azure Developer CLI: $(azd version)"
echo "  Docker: $(docker --version)"
echo "  Git: $(git --version)"
echo ""
echo "Next steps:"
echo "  1. Update .env.local with your Azure OpenAI credentials"
echo "  2. Run 'azd auth login' to authenticate with Azure"
echo "  3. Run 'azd up' to deploy to Azure"
echo "  4. Or run locally:"
echo "     - Terminal 1: cd mcp && python mcp_server.py"
echo "     - Terminal 2: cd agent && python agent.py"
echo ""
