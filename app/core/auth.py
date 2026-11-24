"""Compatibility import for app.auth.frappe_session."""
import sys

from app.auth import frappe_session

sys.modules[__name__] = frappe_session
