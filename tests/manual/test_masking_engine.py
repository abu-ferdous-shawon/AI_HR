import pytest

from app.core import masking
from app.core.masking import Vault, register_confidential
from app.registry import build_profile
from tests.records_helpers import World

RAW = {"name": "t", "record_types": {"leave": {
    "doctype": "Leave Thing", "label": "Leave things", "description": "Leave.",
    "aliases": {"en": ["leave thing"]}, "employee_field": "employee",
    "fields": ["name", "employee", "reason", "status", "days", "pay_hint"],
    "confidential_fields": ["reason", "pay_hint"],
    "group_by_allowed": ["reason", "status"],
    "aggregations": {"total_days": {"op": "sum", "field": "days"},
                     "total_hint": {"op": "sum", "field": "pay_hint"}},   # not marked money
    "scopes": ["self"]}}}


def row(name, reason="fever at home", pay_hint=500, days=1.0):
    return {"name": name, "employee": "EMP-1", "reason": reason, "status": "Approved",
            "days": days, "pay_hint": pay_hint}


@pytest.fixture
def world(monkeypatch):
    saved = set(masking.EXTRA_CONFIDENTIAL)
    w = World(monkeypatch)
    w.profile = build_profile(RAW)
    register_confidential(w.profile.confidential_field_names())
    yield w
    masking.EXTRA_CONFIDENTIAL.clear()
    masking.EXTRA_CONFIDENTIAL.update(saved)


def test_group_names_of_a_confidential_field_are_masked(world):
    world.rows["Leave Thing"] = [row("L1"), row("L2", reason="family event")]
    res = world.summarize(group_by="reason")
    assert {"reason": "fever at home", "records": 1, "value": 1} in res["groups"]
    assert all("group" not in g for g in res["groups"])
    seen = str(Vault().mask(res))
    assert "fever" not in seen and "family" not in seen


def test_group_names_of_an_ordinary_field_stay_readable(world):
    world.rows["Leave Thing"] = [row("L1"), row("L2")]
    res = world.summarize(group_by="status")
    assert res["groups"] == [{"group": "Approved", "records": 2, "value": 2}]
    assert "Approved" in str(Vault().mask(res))


def test_a_total_of_a_confidential_field_is_masked_even_without_the_money_flag(world):
    world.rows["Leave Thing"] = [row("L1", pay_hint=500), row("L2", pay_hint=250)]
    res = world.summarize(aggregation="total_hint")
    assert res["amount"] == 750.0 and "value" not in res
    assert "750" not in str(Vault().mask(res))


def test_a_total_of_an_ordinary_field_stays_visible(world):
    world.rows["Leave Thing"] = [row("L1", days=1.5), row("L2", days=2.0)]
    res = world.summarize(aggregation="total_days")
    assert res["value"] == 3.5 and "3.5" in str(Vault().mask(res))
    