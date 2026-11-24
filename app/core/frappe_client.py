"""Compatibility import for app.frappe_client.client."""
import sys

from app.frappe_client import client

sys.modules[__name__] = client
