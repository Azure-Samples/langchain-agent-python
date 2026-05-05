#!/bin/bash
# Post-provision hook: seed the Postgres database with sample data.
#
# This runs on the developer's machine after `azd up` finishes provisioning.
# It is intentionally tolerant of local environment quirks so that
# infrastructure provisioning is never marked as "failed" because of a
# seeding hiccup. If anything here fails, you can re-run it with:
#
#     azd hooks run postprovision
#
set -uo pipefail

echo "Running post-provision setup..."

# ---- Resolve env ----------------------------------------------------------
ENV_VALUES=$(azd env get-values --output json 2>/dev/null || echo '{}')
POSTGRES_URL=$(printf '%s' "$ENV_VALUES" | jq -r '.POSTGRES_URL // empty')
POSTGRES_HOST=$(printf '%s' "$ENV_VALUES" | jq -r '.POSTGRES_HOST // empty')
POSTGRES_RG=$(printf '%s' "$ENV_VALUES" | jq -r '.AZURE_RESOURCE_GROUP // empty')
AGENT_URL=$(printf '%s' "$ENV_VALUES" | jq -r '.AGENT_URL // empty')
MCP_SERVER_URL=$(printf '%s' "$ENV_VALUES" | jq -r '.MCP_SERVER_URL // empty')

# ---- Seed database --------------------------------------------------------
if [ -z "$POSTGRES_URL" ]; then
  echo "⚠️  POSTGRES_URL not found - skipping database population"
elif [ ! -f "data/product_data.json" ] || [ ! -f "data/reference_data.json" ]; then
  echo "⚠️  Required data files (data/product_data.json, data/reference_data.json) missing."
  echo "    These ship with the repo - check that you cloned a complete copy."
else
  echo "📊 Populating database with sample data..."

  # Add this machine's public IP to the Postgres firewall so the seed script
  # can connect. Idempotent: ignore "already exists".
  if [ -n "$POSTGRES_HOST" ] && [ -n "$POSTGRES_RG" ]; then
    PG_SERVER_NAME="${POSTGRES_HOST%%.*}"
    CLIENT_IP=$(curl -fsS https://api.ipify.org 2>/dev/null || true)
    if [ -n "$CLIENT_IP" ]; then
      echo "   Adding firewall rule for $CLIENT_IP on $PG_SERVER_NAME..."
      az postgres flexible-server firewall-rule create \
        --resource-group "$POSTGRES_RG" \
        --name "$PG_SERVER_NAME" \
        --rule-name "azd-postprovision-$(date +%Y%m%d)" \
        --start-ip-address "$CLIENT_IP" \
        --end-ip-address "$CLIENT_IP" \
        --only-show-errors >/dev/null 2>&1 || echo "   (firewall rule already present, continuing)"
    fi
  fi

  # Pick a working Python interpreter and install asyncpg into a venv if needed.
  # We use a venv to avoid PEP-668 "externally managed environment" failures on
  # macOS Homebrew / Debian-packaged Python.
  PY=$(command -v python3 || command -v python || true)
  if [ -z "$PY" ]; then
    echo "❌ python3 is not on PATH - install Python 3.10+ then re-run: azd hooks run postprovision"
    exit 0  # don't block provisioning
  fi

  VENV_DIR=".azd-postprovision-venv"
  if [ ! -d "$VENV_DIR" ]; then
    echo "   Creating ephemeral venv for seeding..."
    "$PY" -m venv "$VENV_DIR" || {
      echo "❌ Could not create venv. Install python3-venv and re-run: azd hooks run postprovision"
      exit 0
    }
  fi
  # shellcheck disable=SC1091
  . "$VENV_DIR/bin/activate"

  if ! python -c "import asyncpg, pgvector" 2>/dev/null; then
    echo "   Installing asyncpg + pgvector..."
    python -m pip install --quiet --disable-pip-version-check asyncpg pgvector || {
      echo "❌ pip install failed. Re-run later with: azd hooks run postprovision"
      exit 0
    }
  fi

  echo "   Running data/generate_database.py..."
  POSTGRES_URL="$POSTGRES_URL" python data/generate_database.py && \
    echo "✅ Database populated successfully!" || \
    echo "⚠️  Seeding failed. Fix the issue and re-run: azd hooks run postprovision"

  deactivate || true
fi

# ---- Print summary --------------------------------------------------------
echo ""
if [ -n "$AGENT_URL" ]; then
  cat <<EOF
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🚀 Your LangChain Agent is Ready!
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🌐 Web chat:   ${AGENT_URL}/
📊 Chat API:   ${AGENT_URL}/api/chat
   Health:     ${AGENT_URL}/api/health
EOF
  if [ -n "$MCP_SERVER_URL" ]; then
    echo "   MCP Server: ${MCP_SERVER_URL}/mcp"
  fi
  cat <<EOF

💡 Try these in the web interface:
   • What tables are in the database?
   • How many products do we have?
   • Show me the top 5 most expensive products
   • Find hammers using semantic search

🔧 Re-run seeding any time:    azd hooks run postprovision
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOF
fi
echo ""

