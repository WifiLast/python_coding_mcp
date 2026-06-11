"""Mini coding MCP server."""

import sys
from .workspace import create_app, main
from . import app_4722366486172542185216 as app
sys.modules["mini_coding_mcp.app"] = app

