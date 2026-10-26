"""Vercel entry point: the stateless replay demo (sample letters only, no keys, no network calls)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from papertrail.web.serverless import app  # noqa: E402,F401
