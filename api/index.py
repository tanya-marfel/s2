"""Vercel serverless entry point.

Vercel's Python runtime looks for a module-level `handler` that subclasses
BaseHTTPRequestHandler, which is exactly what server.py already defines - so the
same code serves locally (`python3 server.py`) and in production.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import Handler as handler  # noqa: F401,E402
