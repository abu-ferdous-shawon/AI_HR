import pathlib

from app.tools.registry import get_tool_specs, load_all_tools

ROOT = pathlib.Path(__file__).resolve().parents[2]
REQUIRED = """
.env pytest.ini
app/__init__.py app/main.py app/config.py
app/core/__init__.py app/core/agent.py app/core/auth.py app/core/calc.py
app/core/frappe_client.py app/core/language.py app/core/masking.py app/core/media.py
app/core/policy_index.py app/core/reports.py app/core/security.py
app/core/llm/__init__.py app/core/llm/base.py app/core/llm/gemini.py
app/prompts/system_prompt.txt app/static/index.html
app/tools/__init__.py app/tools/common.py app/tools/registry.py
app/tools/hr/__init__.py app/tools/hr/attendance.py app/tools/hr/calculator.py
app/tools/hr/company.py app/tools/hr/holidays.py app/tools/hr/images.py
app/tools/hr/leave.py app/tools/hr/payroll.py app/tools/hr/policy.py
app/tools/hr/profile.py app/tools/hr/reports.py app/tools/hr/team.py
""".split()

missing = [name for name in REQUIRED if not (ROOT / name).is_file()]
print("Missing files:", missing or "none")

load_all_tools()
names = sorted(t["name"] for t in get_tool_specs())
print(f"{len(names)} tools registered:", names)