"""Compatibility import for app.auth.security."""
import sys

from app.auth import security

sys.modules[__name__] = security
