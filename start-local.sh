#!/bin/bash
# Start local development servers

# Kill any existing servers
pkill -f "python3 app.py" 2>/dev/null
sleep 2

# Load environment variables
export $(cat .env.local | grep -v '^#' | grep '=' | xargs)

# Start MCP server in background
echo "Starting MCP server on port 8000..."
cd mcp
nohup python3 app.py > /tmp/mcp.log 2>&1 &
MCP_PID=$!
cd ..

# Wait for MCP server to start
sleep 3

# Start agent server in background
echo "Starting agent server on port 8080..."
cd agent
export PORT=8080
nohup python3 app.py > /tmp/agent.log 2>&1 &
AGENT_PID=$!
cd ..

sleep 3

echo ""
echo "✅ Servers started!"
echo "   MCP Server: http://localhost:8000 (PID: $MCP_PID)"
echo "   Agent/UI:   http://localhost:8080 (PID: $AGENT_PID)"
echo ""
echo "To view logs:"
echo "   tail -f /tmp/mcp.log"
echo "   tail -f /tmp/agent.log"
echo ""
echo "To stop servers:"
echo "   pkill -f 'python3 app.py'"
