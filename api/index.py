import sys
from pathlib import Path

# Ensure backend root is in sys.path
_backend_dir = str(Path(__file__).resolve().parent.parent)
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from app.main import app

# Export app for Vercel Serverless Function entry point
__all__ = ["app"]
