import pytest

from app.registry import RegistryError, build_profile, load_profile

THING = {"doctype": "Thing", "label": "Things", "description": "t",
         "aliases": {"en": ["thing"]}, "employee_field": "employee",
         "fields": ["name", "employee"]}


def raw(**access):
    return {"name": "t", "access": access, "record_types": {"thing": THING}}


def test_defaults_keep_existing_profiles_working():
    profile = build_profile(raw())
    access = profile.access
    assert access.hr_roles == ["HR Manager", "HR User"]
    assert access.manager_roles == [] and access.role_methods == [] and access.role_map == []
    assert (access.user_doctype, access.role_method_param) == ("User", "uid")
    assert profile.employee.designation_field == "designation"


def test_valid_settings_load():
    profile = build_profile(raw(
        hr_roles=["People Ops"], manager_roles=["Line Manager"],
        role_methods=["pkg.module.get_roles"],
        role_map=[{"role": "HR Manager", "designations": ["HR Head"], "users": ["a@x.com"]}]))
    assert profile.access.role_map[0].role == "HR Manager"
    assert profile.access.role_methods == ["pkg.module.get_roles"]


CASES = [
    ("method without a dot", {"role_methods": ["get_roles"]}, "dotted Python paths"),
    ("method with unsafe characters", {"role_methods": ["a.b; drop"]}, "dotted Python paths"),
    ("unsafe user doctype", {"user_doctype": "User; x"}, "unsafe characters"),
    ("unsafe roles field", {"user_roles_field": "roles; x"}, "unsafe field name"),
    ("rule that matches nobody", {"role_map": [{"role": "HR Manager"}]}, "matches nobody"),
    ("rule without a role", {"role_map": [{"designations": ["x"]}]}, "Field required"),
    ("typo in a rule", {"role_map": [{"role": "R", "designation": ["x"]}]},
     "Extra inputs are not permitted"),
    ("blank role name", {"hr_roles": [" "]}, "role names"),
    ("typo in access", {"hr_role": ["HR Manager"]}, "Extra inputs are not permitted"),
]


@pytest.mark.parametrize("label, access, expected", CASES, ids=[c[0] for c in CASES])
def test_invalid_access_settings_are_rejected_clearly(label, access, expected):
    with pytest.raises(RegistryError) as err:
        build_profile(raw(**access))
    assert expected in str(err.value)


def test_the_shipped_profile_declares_how_roles_are_found():
    profile = load_profile("frappe_hrms_v16")
    assert profile.access.role_methods == ["frappe.core.doctype.user.user.get_roles"]
    assert "HR Manager" in profile.access.hr_roles
    assert profile.employee.designation_field == "designation"


def test_a_customer_overlay_changes_only_the_role_map(tmp_path):
    path = tmp_path / "acme.yaml"
    path.write_text(
        "extends: frappe_hrms_v16\nname: acme\naccess:\n  role_map:\n"
        "    - {role: HR Manager, designations: [People Lead]}\n", encoding="utf-8")
    profile = load_profile(str(path))
    assert profile.access.role_map[0].designations == ["People Lead"]
    assert profile.access.role_methods == ["frappe.core.doctype.user.user.get_roles"]
    assert profile.access.hr_roles == ["HR Manager", "HR User"]