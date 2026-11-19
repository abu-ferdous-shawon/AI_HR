"""Keys for caching data under a verified Frappe permission snapshot."""
import hashlib
import json
from collections.abc import Mapping
from typing import Any


def permission_cache_key(
    user_id: str, *, site: str, permissions: Mapping[str, Any],
) -> tuple[str, str]:
    """Return (user_id, permissions_hash) for a site's complete effective grants.

    Supply authoritative, JSON-compatible permissions, including record and field
    restrictions. When only roles are available, skip caching the query result.
    """
    if not user_id.strip() or not site.strip() or not permissions:
        raise ValueError("Site, user and effective permissions are required")
    snapshot = json.dumps(
        [site, dict(permissions)], sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    return user_id, hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
