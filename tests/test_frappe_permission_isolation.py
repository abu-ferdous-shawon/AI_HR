"""Live test against two Frappe users with disjoint access to two records."""
import os

import pytest

from app.auth.credentials import extract_credentials, use_credentials
from app.core.sites import current_site, use_site
from app.frappe_client.client import FrappeError, get_frappe_client

pytestmark = pytest.mark.integration


def test_frappe_enforces_user_record_isolation():
    required = ("DOCTYPE", "USER_A", "USER_B", "TOKEN_A", "TOKEN_B", "RECORD_A", "RECORD_B")
    config = {name: os.environ.get(f"FRAPPE_ISOLATION_{name}", "") for name in required}
    missing = [f"FRAPPE_ISOLATION_{name}" for name, value in config.items() if not value]
    if missing:
        pytest.skip("Set " + ", ".join(missing))

    assert config["USER_A"] != config["USER_B"], "Use two distinct Frappe users"
    assert config["RECORD_A"] != config["RECORD_B"], "Use two distinct records"
    assert "Administrator" not in (config["USER_A"], config["USER_B"])
    site = os.environ.get("FRAPPE_ISOLATION_SITE") or current_site().name
    doctype = config["DOCTYPE"]
    for actor, other in (("A", "B"), ("B", "A"), ("A", "B")):
        credentials = extract_credentials(config[f"TOKEN_{actor}"], None)
        if credentials is None:
            pytest.fail(f"FRAPPE_ISOLATION_TOKEN_{actor} must be a Frappe token or Bearer header")
        own, forbidden = config[f"RECORD_{actor}"], config[f"RECORD_{other}"]
        with use_site(site), use_credentials(credentials):
            client = get_frappe_client()
            assert client.acts_as_user
            assert client.call_method("frappe.auth.get_logged_user") == config[f"USER_{actor}"]
            rows = client.get_list(doctype, filters=[["name", "in", [own, forbidden]]])
            assert {row["name"] for row in rows} == {own}
            assert client.get_doc(doctype, own)["name"] == own
            with pytest.raises(FrappeError) as denied:
                client.get_doc(doctype, forbidden)
            assert denied.value.status_code in {403, 404}
