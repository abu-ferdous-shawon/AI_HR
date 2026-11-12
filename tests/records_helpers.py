from app.core.frappe_client import FrappeError
from app.core.security import UserContext
from app.records import engine, scope
from app.registry import build_profile

ME = UserContext("me@x.com", "EMP-1")


def make_profile():
    return build_profile({
        "name": "test",
        "limits": {"default_limit": 2, "max_limit": 3, "max_rows": 100},
        "record_types": {
            "leave": {
                "doctype": "Leave Thing", "label": "Leave things", "description": "Leave.",
                "aliases": {"en": ["leave thing"], "banglish": ["chuti"]},
                "employee_field": "employee", "date_field": "from_date", "date_end_field": "to_date",
                "status_field": "status", "status_values": ["Open", "Approved"], "docstatus": [0, 1],
                "fields": ["name", "employee", "employee_name", "from_date", "to_date",
                           "status", "days", "reason"],
                "confidential_fields": ["reason"],
                "group_by_allowed": ["status", "employee_name"],
                "aggregations": {"total_days": {"op": "sum", "field": "days", "label": "Total days"},
                                 "avg_days": {"op": "avg", "field": "days"},
                                 "max_days": {"op": "max", "field": "days"}},
                "order_by": "from_date desc", "scopes": ["self", "team", "company"]},
            "slip": {
                "doctype": "Pay Thing", "label": "Pay things", "description": "Pay.",
                "aliases": {"en": ["pay thing"]},
                "employee_field": "employee", "date_field": "start_date", "docstatus": [1],
                "fields": ["name", "employee", "start_date", "net_pay", "dept"],
                "confidential_fields": ["net_pay"], "group_by_allowed": ["dept"],
                "aggregations": {"total_net": {"op": "sum", "field": "net_pay",
                                               "label": "Total net", "money": True}},
                "child_tables": {"earnings": {"fields": ["salary_component", "amount"],
                                              "confidential_fields": ["amount"], "max_rows": 2}},
                "order_by": "start_date desc", "scopes": ["self", "company"]},
            "event": {
                "doctype": "Event Thing", "label": "Event things", "description": "Events.",
                "aliases": {"en": ["event thing"]},
                "employee_via": {"child_doctype": "Event Person", "child_field": "employee",
                                 "parent_field": "parent", "table_field": "people"},
                "fields": ["name", "title"],
                "child_tables": {"people": {"fields": ["employee", "employee_name"], "max_rows": 10}},
                "order_by": "modified desc", "scopes": ["self", "company"]},
            "punch": {
                "doctype": "Punch Thing", "label": "Punch things", "description": "Punches.",
                "aliases": {"en": ["punch thing"]},
                "employee_field": "employee", "date_field": "time", "date_is_datetime": True,
                "fields": ["name", "employee", "time"], "order_by": "time desc",
                "scopes": ["self"]},
        }})


def leave_row(name, status="Approved", days=1.0, **extra):
    row = {"name": name, "employee": "EMP-1", "employee_name": "Me ", "from_date": "2026-03-01",
           "to_date": "2026-03-02", "status": status, "days": days, "reason": "fever",
           "secret_col": "x", "owner": "Administrator"}
    row.update(extra)
    return row


class World:
    """A fake ERP plus fake roles. It also stands in for the Frappe client."""

    def __init__(self, monkeypatch):
        self.profile = make_profile()
        self.rows, self.docs, self.employees = {}, {}, []
        self.error = self.doc_error = None
        self.team, self.hr = [], False
        self.all_calls, self.doc_calls = [], []
        monkeypatch.setattr(scope, "get_service_client", lambda: self)
        monkeypatch.setattr(scope, "get_team", lambda ctx: self.team)
        monkeypatch.setattr(scope, "is_hr_admin", lambda ctx, profile=None: self.hr)

    # the parts of the Frappe client the engine uses
    def get_all(self, doctype, fields=None, filters=None, order_by=None, max_rows=2000):
        self.all_calls.append({"doctype": doctype, "filters": filters,
                               "order_by": order_by, "max_rows": max_rows})
        if self.error:
            raise self.error
        return list(self.rows.get(doctype, []))[:max_rows]

    def get_doc(self, doctype, name):
        self.doc_calls.append((doctype, name))
        if self.doc_error:
            raise self.doc_error
        return self.docs.get(name, {})

    def get_list(self, doctype, fields=None, filters=None, order_by=None, limit=20, start=0):
        return list(self.employees)[:limit]

    @property
    def filters(self):
        return self.all_calls[-1]["filters"]

    def fetch(self, record_type="leave", **kw):
        return engine.fetch_records(ME, record_type, profile=self.profile, **kw)

    def summarize(self, record_type="leave", **kw):
        return engine.summarize_records(ME, record_type, profile=self.profile, **kw)


__all__ = ["ME", "World", "FrappeError", "leave_row", "make_profile"]