"""Vercel serverless entry point — wraps the Flask dashboard (plan item 34/41).

Env vars required on Vercel: DATABASE_URL, DASHBOARD_PASSWORD, DASHBOARD_SECRET.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dashboard.app import app  # noqa: E402  (Vercel's WSGI handler picks this up)
