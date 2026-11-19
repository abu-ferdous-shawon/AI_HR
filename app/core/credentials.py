"""Compatibility import for app.auth.credentials."""
import sys

from app.auth import credentials

sys.modules[__name__] = credentials
