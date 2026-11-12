import re
from typing import Literal

from pydantic import (
    BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator,
)

NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
DOCTYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,60}$")
ORDER_RE = re.compile(
    r"^[a-z_][a-z0-9_]*( (asc|desc))?(, [a-z_][a-z0-9_]*( (asc|desc))?)*$")
SYSTEM_COLUMNS = {"name", "creation", "modified"}

Scope = Literal["self", "team", "company"]


def norm(text) -> str:
    """Lower-case with single spaces: how aliases are compared."""
    return " ".join(str(text).casefold().split())


def _names(values: list[str], what: str) -> list[str]:
    bad = [v for v in values if not NAME_RE.match(v)]
    if bad:
        raise ValueError(f"unsafe field name in {what}: {', '.join(bad)}")
    if len(set(values)) != len(values):
        raise ValueError(f"duplicate names in {what}")
    return values


class Strict(BaseModel):
    """Unknown keys are errors, so a typo in a profile is caught at load."""
    model_config = ConfigDict(extra="forbid")


class Aliases(Strict):
    en: list[str] = Field(default_factory=list)
    bn: list[str] = Field(default_factory=list)
    banglish: list[str] = Field(default_factory=list)

    def all(self) -> list[str]:
        return [*self.en, *self.bn, *self.banglish]


class ChildTable(Strict):
    fields: list[str] = Field(min_length=1)
    confidential_fields: list[str] = Field(default_factory=list)
    max_rows: int = Field(default=50, ge=1, le=200)

    @field_validator("fields", "confidential_fields")
    @classmethod
    def _check(cls, v, info):
        return _names(v, info.field_name)

    @model_validator(mode="after")
    def _subset(self):
        extra = sorted(set(self.confidential_fields) - set(self.fields))
        if extra:
            raise ValueError(
                f"child confidential_fields must be listed in fields: {', '.join(extra)}")
        return self


class Aggregation(Strict):
    op: Literal["count", "sum", "avg", "min", "max"]
    field: str | None = None
    label: str = ""
    money: bool = False

    @model_validator(mode="after")
    def _check(self):
        if self.op != "count" and not self.field:
            raise ValueError(f"aggregation '{self.op}' needs a field")
        if self.field and not NAME_RE.match(self.field):
            raise ValueError("unsafe field name in aggregation")
        if self.money and not self.field:
            raise ValueError("a money aggregation needs a field")
        return self


class EmployeeVia(Strict):
    """For records linked to employees through a child table (e.g. training events).

    table_field is the parent's own table; the engine keeps only the rows of
    employees the caller may see, so co-attendees are never leaked.
    """
    child_doctype: str
    child_field: str = "employee"
    parent_field: str = "parent"
    table_field: str

    @field_validator("child_doctype")
    @classmethod
    def _doctype(cls, v):
        if not DOCTYPE_RE.match(v):
            raise ValueError("doctype contains unsafe characters")
        return v

    @field_validator("child_field", "parent_field", "table_field")
    @classmethod
    def _name(cls, v, info):
        if not NAME_RE.match(v):
            raise ValueError(f"unsafe field name in {info.field_name}")
        return v


class RecordType(Strict):
    doctype: str
    label: str
    description: str
    aliases: Aliases = Field(default_factory=Aliases)

    # How a record belongs to an employee: exactly one of these two.
    employee_field: str | None = None
    employee_via: EmployeeVia | None = None
    # Fixed filters from the profile only; the model can never change them.
    base_filters: list[tuple[str, Literal["=", "!=", "in", "not in"],
                             str | int | float | list[str]]] = Field(default_factory=list)

    date_field: str | None = None
    date_end_field: str | None = None      # with date_field: records overlapping the period
    date_is_datetime: bool = False
    status_field: str | None = None
    status_values: list[str] = Field(default_factory=list)
    docstatus: list[int] | None = None     # None = no docstatus filter

    fields: list[str] = Field(min_length=1)           # the only columns that may be returned
    confidential_fields: list[str] = Field(default_factory=list)
    child_tables: dict[str, ChildTable] = Field(default_factory=dict)

    order_by: str = "modified desc"
    default_limit: int | None = Field(default=None, ge=1, le=500)
    max_limit: int | None = Field(default=None, ge=1, le=500)
    scopes: list[Scope] = Field(default_factory=lambda: ["self"])
    group_by_allowed: list[str] = Field(default_factory=list)
    aggregations: dict[str, Aggregation] = Field(default_factory=dict)

    enabled: bool = True
    verify: bool = False                   # names not confirmed against a real site
    notes: str = ""

    @field_validator("doctype")
    @classmethod
    def _doctype(cls, v):
        if not DOCTYPE_RE.match(v):
            raise ValueError("doctype contains unsafe characters")
        return v

    @field_validator("fields", "confidential_fields", "group_by_allowed")
    @classmethod
    def _field_names(cls, v, info):
        return _names(v, info.field_name)

    @field_validator("employee_field", "date_field", "date_end_field", "status_field")
    @classmethod
    def _optional_name(cls, v, info):
        if v is not None and not NAME_RE.match(v):
            raise ValueError(f"unsafe field name in {info.field_name}")
        return v

    @field_validator("child_tables", "aggregations")
    @classmethod
    def _keys(cls, v, info):
        bad = [k for k in v if not NAME_RE.match(k)]
        if bad:
            raise ValueError(f"unsafe names in {info.field_name}: {', '.join(bad)}")
        return v

    @field_validator("docstatus")
    @classmethod
    def _docstatus(cls, v):
        if v is not None and (not v or any(x not in (0, 1, 2) for x in v)):
            raise ValueError("docstatus values must be 0, 1 or 2")
        return v

    @field_validator("scopes")
    @classmethod
    def _scopes(cls, v):
        if not v or len(set(v)) != len(v):
            raise ValueError("scopes must be a non-empty list without repeats")
        return v

    @field_validator("order_by")
    @classmethod
    def _order(cls, v):
        if not ORDER_RE.match(v):
            raise ValueError(f"order_by is not safe: {v!r}")
        return v

    @model_validator(mode="after")
    def _consistent(self):
        problems = []
        if bool(self.employee_field) == bool(self.employee_via):
            problems.append("set exactly one of employee_field or employee_via")
        if self.employee_via and self.employee_via.table_field not in self.child_tables:
            problems.append(
                f"employee_via.table_field '{self.employee_via.table_field}' must be a "
                "child table of this record type (add it under child_tables)")
        if self.date_end_field and not self.date_field:
            problems.append("date_end_field needs date_field")
        if self.status_values and not self.status_field:
            problems.append("status_values needs status_field")
        if self.status_field and not self.status_values:
            problems.append("status_field needs status_values")

        allowed = set(self.fields)
        missing = [f for f in self.confidential_fields if f not in allowed]
        if missing:
            problems.append(f"confidential_fields must be listed in fields: {', '.join(missing)}")
        missing = [f for f in self.group_by_allowed if f not in allowed]
        if missing:
            problems.append(f"group_by_allowed must be listed in fields: {', '.join(missing)}")
        for name, agg in self.aggregations.items():
            if agg.field and agg.field not in allowed:
                problems.append(f"aggregation '{name}' uses field '{agg.field}' which is not in fields")
            if agg.money and agg.field not in self.confidential_fields:
                problems.append(f"aggregation '{name}' is money but its field is not in confidential_fields")
        for part in self.order_by.split(","):
            column = part.split()[0]
            if column not in allowed and column not in SYSTEM_COLUMNS:
                problems.append(f"order_by column '{column}' is not in fields")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class EmployeeConfig(Strict):
    """Where the ERP keeps employees. Core code never names these."""
    doctype: str = "Employee"
    name_field: str = "employee_name"
    user_field: str = "user_id"
    reports_to_field: str = "reports_to"
    status_field: str = "status"
    active_value: str = "Active"
    department_field: str = "department"
    designation_field: str = "designation"

    @field_validator("doctype")
    @classmethod
    def _doctype(cls, v):
        if not DOCTYPE_RE.match(v):
            raise ValueError("doctype contains unsafe characters")
        return v

    @field_validator("name_field", "user_field", "reports_to_field",
                     "status_field", "department_field", "designation_field")
    @classmethod
    def _name(cls, v, info):
        if not NAME_RE.match(v):
            raise ValueError(f"unsafe field name in {info.field_name}")
        return v


METHOD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$")


class RoleRule(Strict):
    """Fallback: give a role to anyone matching ANY of these lists (used when the ERP cannot tell)."""
    role: str = Field(min_length=1, max_length=60)
    employees: list[str] = Field(default_factory=list)
    users: list[str] = Field(default_factory=list)
    designations: list[str] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_a_matcher(self):
        if not (self.employees or self.users or self.designations or self.departments):
            raise ValueError(f"role rule for '{self.role}' matches nobody: list at least one "
                             "of employees, users, designations or departments")
        return self


class AccessConfig(Strict):
    hr_roles: list[str] = Field(default_factory=lambda: ["HR Manager", "HR User"])
    manager_roles: list[str] = Field(default_factory=list)   # empty = anyone with direct reports
    user_doctype: str = "User"
    user_roles_field: str = "roles"
    role_name_field: str = "role"
    role_method_param: str = "uid"
    role_methods: list[str] = Field(default_factory=list)    # whitelisted methods that return a person's roles
    role_map: list[RoleRule] = Field(default_factory=list)

    @field_validator("hr_roles", "manager_roles")
    @classmethod
    def _role_names(cls, v):
        if any(not r.strip() or len(r) > 60 for r in v):
            raise ValueError("role names must be 1 to 60 characters")
        return v

    @field_validator("user_doctype")
    @classmethod
    def _doctype(cls, v):
        if not DOCTYPE_RE.match(v):
            raise ValueError("doctype contains unsafe characters")
        return v

    @field_validator("user_roles_field", "role_name_field", "role_method_param")
    @classmethod
    def _field_name(cls, v, info):
        if not NAME_RE.match(v):
            raise ValueError(f"unsafe field name in {info.field_name}")
        return v

    @field_validator("role_methods")
    @classmethod
    def _methods(cls, v):
        bad = [m for m in v if not METHOD_RE.match(m)]
        if bad:
            raise ValueError("role_methods must be dotted Python paths such as "
                             f"pkg.module.function: {', '.join(bad)}")
        return v


class Limits(Strict):
    default_limit: int = Field(default=20, ge=1, le=200)
    max_limit: int = Field(default=100, ge=1, le=500)
    max_rows: int = Field(default=2000, ge=100, le=20000)   # cap for totals computed in Python


class Profile(Strict):
    name: str
    erp: str = "frappe"
    description: str = ""
    employee: EmployeeConfig = Field(default_factory=EmployeeConfig)
    access: AccessConfig = Field(default_factory=AccessConfig)
    limits: Limits = Field(default_factory=Limits)
    record_types: dict[str, RecordType]

    _aliases: dict[str, str] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _finish(self):
        problems = []
        for key, rt in self.record_types.items():
            if not NAME_RE.match(key):
                problems.append(f"record type key '{key}' must be lower_snake_case")
            rt.default_limit = rt.default_limit or self.limits.default_limit
            rt.max_limit = rt.max_limit or self.limits.max_limit
            if rt.default_limit > rt.max_limit:
                problems.append(f"{key}: default_limit ({rt.default_limit}) is larger "
                                f"than max_limit ({rt.max_limit})")
            rt.aggregations.setdefault("count", Aggregation(op="count", label="Number of records"))

        index: dict[str, str] = {}
        for key, rt in self.record_types.items():
            if not rt.enabled:
                continue
            for text in {norm(x) for x in [key, rt.label, *rt.aliases.all()] if str(x).strip()}:
                owner = index.setdefault(text, key)
                if owner != key:
                    problems.append(f"'{text}' is used by both {owner} and {key}")
        if not index:
            problems.append("at least one record type must be enabled")
        if problems:
            raise ValueError("; ".join(problems))
        self._aliases = index
        return self

    # ---- lookups ----

    def enabled_types(self) -> dict[str, RecordType]:
        return {k: v for k, v in self.record_types.items() if v.enabled}

    def get(self, key: str) -> RecordType:
        return self.record_types[key]

    def resolve(self, text: str) -> str | None:
        """Record type key for a key, label or alias in any language, else None."""
        return self._aliases.get(norm(text))

    def catalog(self) -> list[dict]:
        return [{"key": k, "label": rt.label, "description": rt.description,
                 "scopes": list(rt.scopes)} for k, rt in self.enabled_types().items()]

    def unverified(self) -> list[str]:
        return [k for k, rt in self.enabled_types().items() if rt.verify]

    def confidential_field_names(self) -> set[str]:
        names: set[str] = set()
        for rt in self.enabled_types().values():
            names.update(rt.confidential_fields)
            for table in rt.child_tables.values():
                names.update(table.confidential_fields)
        return names