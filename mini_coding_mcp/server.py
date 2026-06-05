from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from mini_coding_mcp.workspace import MiniWorkspace, create_app, main
else:
    from .workspace import MiniWorkspace, create_app, main

__all__ = ["MiniWorkspace", "create_app", "main"]


if __name__ == "__main__":
    main()
