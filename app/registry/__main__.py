import sys

from app.config import settings
from app.registry.loader import RegistryError, load_profile

ref = sys.argv[1] if len(sys.argv) > 1 else settings.registry_profile
try:
    profile = load_profile(ref)
except RegistryError as err:
    raise SystemExit(f"INVALID profile '{ref}':\n{err}")

enabled = profile.enabled_types()
print(f"Profile '{profile.name}' OK: {len(enabled)} record types enabled, "
      f"{len(profile.record_types) - len(enabled)} disabled")
print("Names to confirm on your ERP (verify: true):",
      ", ".join(profile.unverified()) or "none")