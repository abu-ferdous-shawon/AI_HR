import pytest

from app.core import masking
from app.core.masking import Vault, is_confidential, register_confidential
from app.registry import load_profile
from tests.records_helpers import World

PROFILE = load_profile("frappe_hrms_v16")
TYPES = list(PROFILE.enabled_types())
AGGREGATIONS = [(key, name) for key, rt in PROFILE.enabled_types().items()
                for name in rt.aggregations]


@pytest.fixture
def world(monkeypatch):
    saved = set(masking.EXTRA_CONFIDENTIAL)
    w = World(monkeypatch)
    w.profile = PROFILE
    register_confidential(PROFILE.confidential_field_names())
    yield w
    masking.EXTRA_CONFIDENTIAL.clear()
    masking.EXTRA_CONFIDENTIAL.update(saved)


def mark(*parts: str) -> str:
    return "SENTINEL_" + "_".join(parts) + "_END"


@pytest.mark.parametrize("key", TYPES)
def test_confidential_values_of_every_record_type_stay_hidden(world, key):
    rt = PROFILE.get(key)
    expected = []                                   # (sentinel, field name)

    row = {f: mark(key, f) for f in rt.fields}
    row["name"] = f"R-{key}"
    expected += [(row[f], f) for f in rt.fields if f != "name"]

    doc = {}
    for table, child in rt.child_tables.items():
        line = {f: mark(key, table, f) for f in child.fields}
        link = rt.employee_via.child_field if rt.employee_via and \
            table == rt.employee_via.table_field else None
        if link:
            line[link] = "EMP-1"                    # the caller's own line, so it is kept
        expected += [(line[f], f) for f in child.fields if f != link]
        doc[table] = [line]

    world.rows[rt.doctype] = [row]
    world.docs[row["name"]] = doc
    res = world.fetch(key)
    assert res["outcome"] == "records"
    seen = str(Vault().mask(res))

    for sentinel, field in expected:
        if is_confidential(field):
            assert sentinel not in seen, f"{key}: confidential '{field}' reached the model"
        else:
            assert sentinel in seen, f"{key}: readable '{field}' was hidden or dropped"


@pytest.mark.parametrize("key, name", AGGREGATIONS, ids=[f"{k}.{n}" for k, n in AGGREGATIONS])
def test_totals_of_confidential_fields_never_reach_the_model(world, key, name):
    rt = PROFILE.get(key)
    agg = rt.aggregations[name]
    rows = []
    for i in range(2):
        row = {f: f"X{i}" for f in rt.fields}
        row["name"] = f"R{i}"
        if agg.field:
            row[agg.field] = "7777.25"
        rows.append(row)
    world.rows[rt.doctype] = rows

    res = world.summarize(key, aggregation=name)
    seen = str(Vault().mask(res))
    if agg.op == "count":
        assert res["value"] == 2 and "'value': 2" in seen
    elif is_confidential(agg.field):
        assert "amount" in res and "value" not in res
        assert "7777" not in seen and "15554" not in seen
    else:
        assert res["value"] == 15554.5 and "15554.5" in seen