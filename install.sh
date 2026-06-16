#!/bin/bash
# Install Buffer MCP server dependencies and verify setup
# Run from the buffer-mcp directory

set -e

echo "Installing Buffer MCP dependencies..."
pip install --break-system-packages mcp httpx pydantic 2>/dev/null || pip install mcp httpx pydantic

echo ""
echo "Verifying server compiles..."
python3 -m py_compile server.py && echo "OK: server.py compiles cleanly"

echo ""
echo "Testing connection to Buffer API..."
if [ -z "$BUFFER_ACCESS_TOKEN" ]; then
    echo "WARNING: BUFFER_ACCESS_TOKEN not set. Set it before running the server:"
    echo "  export BUFFER_ACCESS_TOKEN='your-token-here'"
else
    python3 -c "
import asyncio, sys
sys.path.insert(0, '.')
from server import buffer_list_channels
result = asyncio.run(buffer_list_channels())
print(result)
"
fi

echo ""
echo "To add to Claude Desktop, add this to your MCP config:"
echo ""
echo '{
  "mcpServers": {
    "buffer": {
      "command": "python3",
      "args": ["'$(pwd)'/server.py"],
      "env": {
        "BUFFER_ACCESS_TOKEN": "YOUR_TOKEN_HERE"
      }
    }
  }
}'
echo ""
echo "Done."
