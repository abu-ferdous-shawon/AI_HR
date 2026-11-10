import inspect

from app.core import security
from app.core.security import UserContext
from app.tools import registry

MANAGER = UserContext("mgr@x.com", "HR-EMP-00004")


def test_every_tool_that_takes_an_employee_id_refuses_outsiders(monkeypatch):
    monkeypatch.setattr(security, "get_team", lambda ctx: [])  # no team at all
    guarded = [t for t in registry.get_tool_specs()
               if "employee_id" in t["parameters"].get("properties", {})]
    assert guarded, "expected at least get_attendance to take an employee_id"
    for spec in guarded:
        res = registry.run_tool(spec["name"], {"employee_id": "HR-EMP-99999"}, MANAGER)
        assert "error" in res, f"{spec['name']} answered about an outsider!"


def test_declared_inputs_match_what_each_function_accepts():
    for t in registry._TOOLS.values():
        accepted = set(inspect.signature(t.func).parameters) - {"ctx"}
        declared = set(t.parameters.get("properties", {}))
        assert accepted == declared, f"{t.name}: model sees {declared}, function takes {accepted}"