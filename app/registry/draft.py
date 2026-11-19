import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.config import settings
from app.frappe_client.client import FrappeError, get_service_client
from app.registry.loader import (
    RegistryError, extends_reference, load_profile, load_with_overlay,
)
from app.registry.models import Profile, RecordType
from app.registry.probe import TypeReport, analyse, observe

UNCHECKED = "could not be checked"
SUMMARY_ORDER = [("disabled", "DISABLED"), ("adjusted", "ADJUSTED"),
                 ("confirmed", "CONFIRMED"), ("kept", "NOT CHANGED, NEEDS A LOOK")]


@dataclass
class TypeChange:
    key: str
    action: str          # disabled, adjusted, confirmed, unchanged or kept
    overlay: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _names(report: TypeReport, place: str) -> list[str]:
    return [g.name for g in report.gaps if g.place == place]


def _order_by(order_by: str, gone: set[str]) -> str:
    kept = [part.strip() for part in order_by.split(",") if part.split()[0] not in gone]
    return ", ".join(kept) if kept else "modified desc"


def _disabled(key: str, why: str) -> TypeChange:
    return TypeChange(key, "disabled", {"enabled": False}, [why])


def plan_type(key: str, rt: RecordType, report: TypeReport,
              disable_denied: bool = False) -> TypeChange:
    """What to change in the profile so this type matches the ERP."""
    if report.access == "missing":
        return _disabled(key, "the doctype does not exist on this ERP")
    if report.access == "denied":
        if disable_denied:
            return _disabled(key, "the integration key cannot read this doctype")
        return TypeChange(key, "kept", {}, [
            "the key cannot read this doctype, so it was not checked (in user_session mode "
            "the person's own permissions decide; --disable-denied turns it off)"])
    if report.access != "ok":
        return TypeChange(key, "kept", {}, ["the ERP did not answer properly: run this again"])
    if report.verdict == "UNKNOWN":
        return TypeChange(key, "kept", {}, ["no records and no field list: create one test "
                                            "record in the ERP, then run this again"])
    blocking = [g for g in report.gaps if g.blocking]
    if blocking:
        why = "; ".join(f"missing {g.place}: {g.name}" for g in blocking)
        return _disabled(key, f"it cannot work on this ERP ({why})")

    overlay: dict = {}
    notes: list[str] = []

    gone = set(_names(report, "fields"))
    if gone:
        overlay["fields"] = [f for f in rt.fields if f not in gone]
        confidential = [f for f in rt.confidential_fields if f not in gone]
        if confidential != rt.confidential_fields:
            overlay["confidential_fields"] = confidential
        groups = [f for f in rt.group_by_allowed if f not in gone]
        if groups != rt.group_by_allowed:
            overlay["group_by_allowed"] = groups
        dead = [n for n, agg in rt.aggregations.items() if agg.field in gone]
        if dead:
            overlay["aggregations"] = {n: None for n in dead}
        order = _order_by(rt.order_by, gone)
        if order != rt.order_by:
            overlay["order_by"] = order
        notes.append("dropped fields the ERP does not have: " + ", ".join(sorted(gone)))
        if dead:
            notes.append("removed totals that used them: " + ", ".join(dead))

    if _names(report, "date_field"):
        overlay["date_field"], overlay["date_end_field"] = None, None
        notes.append("removed the date filter (the date field does not exist)")
    elif _names(report, "date_end_field"):
        overlay["date_end_field"] = None
        notes.append("removed the end-date field (it does not exist)")

    if _names(report, "status_field"):
        overlay["status_field"], overlay["status_values"] = None, []
        notes.append("removed the status filter (the status field does not exist)")
    elif report.status_unknown:
        overlay["status_values"] = [*rt.status_values, *report.status_unknown]
        notes.append("added status values the ERP uses: " + ", ".join(report.status_unknown))

    tables: dict = {}
    for name in _names(report, "child_tables"):
        tables[name] = None
    missing_in: dict[str, set[str]] = {}
    for gap in report.gaps:
        if gap.place.startswith("table "):
            missing_in.setdefault(gap.place[len("table "):], set()).add(gap.name)
    for table, names in missing_in.items():
        if table in tables:
            continue
        ct = rt.child_tables[table]
        kept = [f for f in ct.fields if f not in names]
        if not kept:
            tables[table] = None
            continue
        entry: dict = {"fields": kept}
        confidential = [f for f in ct.confidential_fields if f not in names]
        if confidential != ct.confidential_fields:
            entry["confidential_fields"] = confidential
        tables[table] = entry
    if tables:
        via = rt.employee_via.table_field if rt.employee_via else None
        if via and tables.get(via, 0) is None:
            return _disabled(key, f"its employee link table '{via}' cannot be used on this ERP")
        overlay["child_tables"] = tables
        notes.append("changed child tables: " + ", ".join(sorted(tables)))

    if rt.verify:
        if any(UNCHECKED in n for n in report.notes):
            notes.append("still marked verify: true because its child tables could not be checked")
        else:
            overlay["verify"] = False

    if set(overlay) - {"verify"}:
        action = "adjusted"
    elif overlay:
        action = "confirmed"
    else:
        action = "kept" if notes else "unchanged"
    return TypeChange(key, action, overlay, notes)


def plan(profile: Profile, reports: dict[str, TypeReport],
         disable_denied: bool = False) -> list[TypeChange]:
    return [plan_type(key, rt, reports[key], disable_denied)
            for key, rt in profile.enabled_types().items() if key in reports]


def build_overlay(name: str, changes: list[TypeChange]) -> dict:
    return {"name": name,
            "record_types": {c.key: c.overlay for c in changes if c.overlay}}


def render_yaml(extends: str, overlay: dict, changes: list[TypeChange]) -> str:
    lines = ["# Draft profile written by: python -m app.registry.draft",
             "# Review it, then set REGISTRY_PROFILE to this file's path.",
             "# A value of null removes that entry from the profile it extends.",
             f"extends: {json.dumps(extends)}",
             f"name: {json.dumps(overlay['name'])}"]
    types = overlay["record_types"]
    if not types:
        lines.append("record_types: {}")
    else:
        lines.append("record_types:")
        for change in changes:
            if change.key not in types:
                continue
            lines += [f"  # {change.key}: {note}" for note in change.notes]
            block = yaml.safe_dump({change.key: change.overlay}, sort_keys=False,
                                   allow_unicode=True, default_flow_style=None, width=100)
            lines += ["  " + line for line in block.rstrip("\n").splitlines()]
    return "\n".join(lines) + "\n"


def summary(changes: list[TypeChange], name: str, base: str) -> str:
    lines = [f"Draft profile '{name}', laid over '{base}'", ""]
    for action, title in SUMMARY_ORDER:
        group = [c for c in changes if c.action == action]
        if not group:
            continue
        lines.append(f"{title} ({len(group)})")
        for change in group:
            text = "; ".join(change.notes) or "verify cleared: every name matched the ERP"
            lines.append(f"  {change.key}: {text}")
        lines.append("")
    same = sum(1 for c in changes if c.action == "unchanged")
    lines.append(f"{same} types already matched the ERP and need no change.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Draft a customer profile from what the ERP really looks like. "
                    "Shows the changes first; writes a file only with --write.")
    parser.add_argument("--profile", help="profile to start from (default: REGISTRY_PROFILE)")
    parser.add_argument("--name", help="name of the new profile")
    parser.add_argument("--write", metavar="PATH", help="save the draft to this file")
    parser.add_argument("--force", action="store_true", help="overwrite an existing file")
    parser.add_argument("--disable-denied", action="store_true",
                        help="also disable types the integration key cannot read")
    parser.add_argument("--type", action="append", dest="types", help="check only this record type")
    args = parser.parse_args(argv)

    base_ref = args.profile or settings.registry_profile
    try:
        base = load_profile(base_ref)
    except RegistryError as err:
        print(f"The profile is invalid:\n{err}")
        return 2
    keys = list(base.enabled_types())
    chosen = args.types or keys
    unknown = [k for k in chosen if k not in keys]
    if unknown:
        print(f"Unknown record types: {', '.join(unknown)}. Available: {', '.join(keys)}")
        return 2

    client = get_service_client()
    try:
        who = client.call_method("frappe.auth.get_logged_user")
    except FrappeError as exc:
        print(f"Cannot reach the ERP: {exc}")
        return 2
    print(f"Reading the ERP as '{who}' ...", file=sys.stderr)
    reports: dict[str, TypeReport] = {}
    for key in keys:
        if key in chosen:
            print(f"  checking {key} ...", file=sys.stderr, flush=True)
            reports[key] = analyse(key, base.get(key), observe(client, base.get(key)))

    changes = plan(base, reports, args.disable_denied)
    name = args.name or f"{base.name}-adjusted"
    overlay = build_overlay(name, changes)
    try:
        load_with_overlay(base_ref, overlay)          # the draft must be a valid profile
    except RegistryError as err:
        print(f"The draft is not a valid profile, so nothing was written:\n{err}")
        return 2

    extends = extends_reference(base_ref)
    print(summary(changes, name, extends))
    if not overlay["record_types"]:
        print("\nNo changes are needed.")
        return 0
    text = render_yaml(extends, overlay, changes)
    if not args.write:
        print("\n--- draft profile (nothing was written; add --write PATH to save it) ---\n" + text)
        return 0
    path = Path(args.write).expanduser()
    if path.exists() and not args.force:
        print(f"\n{path} already exists. Use --force to overwrite it.")
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"\nWritten to {path}\nNext: set REGISTRY_PROFILE={path} in .env, then run "
          f"python -m app.registry {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())