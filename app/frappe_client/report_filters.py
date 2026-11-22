"""Extract literal Frappe report filter declarations without executing JavaScript."""
import re
from functools import lru_cache

import tree_sitter_javascript
from tree_sitter import Language, Parser


@lru_cache
def _javascript() -> Language:
    return Language(tree_sitter_javascript.language())


def _source(node) -> str:
    return node.text.decode() if node is not None else ""


def _properties(node) -> dict:
    return {
        _source(pair.child_by_field_name("key")).strip("'\""): pair.child_by_field_name("value")
        for pair in node.named_children if pair.type == "pair"
    }


def _literal(node) -> str:
    if node is None:
        return ""
    if node.type == "call_expression" and _source(node.child_by_field_name("function")) == "__":
        args = node.child_by_field_name("arguments").named_children
        node = args[0] if args else None
    if node is not None and node.type == "string":
        # Decode the escapes commonly present in Frappe filters without eval.
        return re.sub(r"\\([\\'\"nrt])", lambda m: {"n": "\n", "r": "\r", "t": "\t"}.get(
            m[1], m[1]), _source(node)[1:-1])
    return ""


def static_report_filters(script: str, report_name: str) -> list[dict]:
    if not script:
        return []
    root = Parser(_javascript()).parse(script.encode()).root_node
    filters = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if (left is not None and left.type == "subscript_expression"
                    and _source(left.child_by_field_name("object")) == "frappe.query_reports"
                    and _literal(left.child_by_field_name("index")) == report_name
                    and right is not None and right.type == "object"):
                array = _properties(right).get("filters")
                if array is not None and array.type == "array":
                    filters.extend(_static_fields(array))
                continue
        stack.extend(reversed(node.named_children))
    return filters


def _static_fields(array) -> list[dict]:
    fields = []
    for obj in array.named_children:
        if obj.type != "object":
            continue
        props = _properties(obj)
        fieldname = _literal(props.get("fieldname"))
        if not fieldname:
            continue
        options = props.get("options")
        if options is not None and options.type == "array":
            values = (_literal(item) or (_literal(_properties(item).get("value"))
                                         if item.type == "object" else "")
                      for item in options.named_children)
            choices = "\n".join(value for value in values if value)
        else:
            choices = _literal(options)
        required = _source(props.get("reqd"))
        fields.append({
            "fieldname": fieldname, "label": _literal(props.get("label")),
            "fieldtype": _literal(props.get("fieldtype")), "options": choices,
            "reqd": required in {"1", "true"},
        })
    return fields
