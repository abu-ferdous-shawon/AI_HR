import ast
import re
from pathlib import Path

FULL = [
    "app/core/agent.py",
    "app/prompts/system_prompt.txt",
    "app/tools/hr/leave.py",
    "app/tools/hr/reports.py",
    "app/core/security.py",
]
FULL_IF_EXISTS = ["app/tools/__init__.py", "app/tools/registry.py", "app/tools/base.py"]
OUTLINE = [
    "app/core/auth.py",
    "app/core/masking.py",
    "app/core/frappe_client.py",
    "app/core/reports.py",
    "app/tools/hr/payroll.py",
    "app/tools/hr/team.py",
    "app/tools/hr/profile.py",
]

SECRET = re.compile(r"(AIza[0-9A-Za-z_\-]{20,}|gsk_[0-9A-Za-z]{20,}|sk-[0-9A-Za-z_\-]{20,})")
OUT = Path("context_pack.txt")


def tokens(text):
    return len(text) // 4


def strip(text):
    kept = [ln.rstrip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    return "\n".join(kept)


def short(text, limit=170):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def signature(node):
    kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    decorators = "".join(f"@{short(ast.unparse(d))}\n" for d in node.decorator_list)
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    doc = ast.get_docstring(node)
    note = f"   # {doc.splitlines()[0]}" if doc else ""
    return f"{decorators}{kind} {node.name}({ast.unparse(node.args)}){returns}{note}"


def outline(text):
    tree = ast.parse(text)
    lines = []
    module_doc = ast.get_docstring(tree)
    if module_doc:
        lines.append(f'"""{module_doc.splitlines()[0]}"""')
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.Assign):
            names = ", ".join(ast.unparse(t) for t in node.targets)
            lines.append(f"{names} = {short(ast.unparse(node.value), 80)}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.append(signature(node))
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            lines.append(f"class {node.name}({bases}):")
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    lines.append("    " + signature(item).replace("\n", "\n    "))
                elif isinstance(item, ast.AnnAssign):
                    lines.append(f"    {short(ast.unparse(item), 100)}")
    return "\n".join(lines)


def main():
    parts, report = [], []
    plan = [("FULL", p) for p in FULL + [f for f in FULL_IF_EXISTS if Path(f).exists()]]
    plan += [("OUTLINE", p) for p in OUTLINE]
    for mode, name in plan:
        path = Path(name)
        if not path.exists():
            report.append(f"  missing  {name}")
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        try:
            body = outline(raw) if mode == "OUTLINE" and path.suffix == ".py" else strip(raw)
        except SyntaxError:
            body = strip(raw)
        body = SECRET.sub("[REDACTED]", body)
        parts.append(f"===== {mode}: {name} =====\n{body}\n")
        report.append(f"  {tokens(body):>6} tokens  {mode:<7} {name}")
    OUT.write_text("\n".join(parts), encoding="utf-8")
    print("\n".join(report))
    print(f"\nWrote {OUT}  (about {tokens(OUT.read_text(encoding='utf-8'))} tokens in total)")


if __name__ == "__main__":
    main()