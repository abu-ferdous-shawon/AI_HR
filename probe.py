import os, json, datetime, requests

URL = os.environ["FRAPPE_URL"].rstrip("/")
H = {"Authorization": f"token {os.environ['FRAPPE_API_KEY']}:{os.environ['FRAPPE_API_SECRET']}"}
out = []

def call(label, path, params=None):
    try:
        r = requests.get(URL + path, headers=H, params=params, timeout=30)
        try:
            body = json.dumps(r.json(), indent=2, ensure_ascii=False)
        except Exception:
            body = r.text[:2000]
        out.append(f"--- {label}\nSTATUS {r.status_code}\n{body}\n")
        return r
    except Exception as e:
        out.append(f"--- {label}\nFAILED: {e}\n")
        return None

ALL = json.dumps(["*"])

call("1 logged user", "/api/method/frappe.auth.get_logged_user")
call("2 leave types", "/api/resource/Leave Type",
     {"fields": json.dumps(["name", "max_leaves_allowed", "is_lwp"]), "limit_page_length": 20})
emp = call("3 one employee, all fields", "/api/resource/Employee",
           {"fields": ALL, "limit_page_length": 1})
call("4 one leave application, all fields", "/api/resource/Leave Application",
     {"fields": ALL, "limit_page_length": 1})
call("5 one leave allocation, all fields", "/api/resource/Leave Allocation",
     {"fields": ALL, "limit_page_length": 1})
call("6 one attendance record, all fields", "/api/resource/Attendance",
     {"fields": ALL, "limit_page_length": 1})
call("7 holiday lists", "/api/resource/Holiday List", {"limit_page_length": 10})

try:
    emp_id = emp.json()["data"][0]["name"]
    call(f"8 leave balance for {emp_id}",
         "/api/method/hrms.hr.doctype.leave_application.leave_application.get_leave_details",
         {"employee": emp_id, "date": datetime.date.today().isoformat()})
except Exception as e:
    out.append(f"--- 8 skipped, could not read an employee ID: {e}\n")

with open("output.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("Done. Results saved in output.txt")