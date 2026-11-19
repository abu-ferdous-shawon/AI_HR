import pytest

from app.auth.cache_keys import permission_cache_key


def test_cache_keys_separate_user_site_record_and_field_permissions():
    grants = {"Company": ["A"], "permlevels": [0]}
    key = permission_cache_key("alice", site="primary", permissions=grants)
    assert key == permission_cache_key(
        "alice", site="primary", permissions={"permlevels": [0], "Company": ["A"]},
    )
    assert key != permission_cache_key("bob", site="primary", permissions=grants)
    assert key != permission_cache_key("alice", site="secondary", permissions=grants)
    assert key != permission_cache_key(
        "alice", site="primary", permissions={"Company": ["B"], "permlevels": [0]},
    )
    assert key != permission_cache_key(
        "alice", site="primary", permissions={"Company": ["A"], "permlevels": [0, 1]},
    )
    with pytest.raises(ValueError):
        permission_cache_key("alice", site="primary", permissions={})
