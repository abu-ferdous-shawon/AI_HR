"""Compatibility import for app.frappe_client.service_purpose."""
import sys

from app.frappe_client import service_purpose

sys.modules[__name__] = service_purpose
