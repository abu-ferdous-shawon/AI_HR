"""Compatibility import for app.auth.roles."""
import sys

from app.auth import roles

sys.modules[__name__] = roles
