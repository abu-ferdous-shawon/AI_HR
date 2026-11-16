import sys

from app.core.frappe_client import FrappeClient, FrappeError, get_service_client
from app.registry import get_profile
from tests.manual._login import HOW_TO, credentials_from_env

DEFAULT_DOCTYPES = ["Employee", "Attendance", "Leave Application", "Salary Slip",
                    "Expense Claim", "Appraisal", "Employee Grievance", "Contract"]
CAP = 200


def count_of(client, doctype: str) -> tuple[int | None, str]:
    """(count, label). The count is None when the ERP refused or failed."""
    try:
        rows = client.get_all(doctype, ["name"], max_rows=CAP + 1)
    except FrappeError as exc:
        kind = "refused" if exc.status_code in (401, 403) else "failed"
        return None, f"{kind} ({exc.status_code})"
    return len(rows), (f"{CAP}+" if len(rows) > CAP else str(len(rows)))


def compare(key_result, user_result) -> str:
    """What the ERP did for the person, compared with the key."""
    key_count, user_count = key_result[0], user_result[0]
    if key_count is None and user_count is None:
        return "no access either way"
    if user_count is None:
        return "RESTRICTED (refused for the person)"
    if key_count is None:
        return "person sees more than the key"
    if user_count < key_count:
        return "RESTRICTED (fewer records)"
    if user_count > key_count:
        return "person sees more than the key"
    return "same view"


def conclusion(verdicts: list[str]) -> str:
    if any(v.startswith("RESTRICTED") for v in verdicts):
        return ("Frappe applies this person's own permissions, so FRAPPE_AUTH_MODE=user_session "
                "works on this ERP.")
    if any("more than the key" in v for v in verdicts):
        return ("The person can read things the integration key cannot. Give the key's role "
                "read access to those types, or tools that use the key will see less.")
    return ("This person sees exactly what the key sees. They may be an administrator or HR, "
            "or Frappe may not restrict these types. Repeat with a plain employee login to "
            "see the difference.")


def logged_in_as(client) -> str | None:
    try:
        return client.call_method("frappe.auth.get_logged_user")
    except FrappeError:
        return None


def main() -> None:
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    names = [a for a in sys.argv[1:] if not a.startswith("--")]
    credentials = credentials_from_env()
    if credentials is None:
        raise SystemExit(HOW_TO)
    if "--all" in flags:
        names = sorted({rt.doctype for rt in get_profile().enabled_types().values()})
    names = names or DEFAULT_DOCTYPES

    service, person = get_service_client(), FrappeClient(credentials=credentials)
    try:
        key_user, person_user = logged_in_as(service), logged_in_as(person)
        if person_user is None:
            raise SystemExit("The ERP did not accept the person's login. Check HR_TEST_TOKEN "
                             "or HR_TEST_SID (a session id ends when the person logs out).")
        if key_user and key_user == person_user:
            print("WARNING: this login is the integration key's own account, so the "
                  "comparison below means nothing.\n")

        print(f"{'DocType':28} {'as the key':>12} {'as the person':>16}   verdict")
        verdicts = []
        for doctype in names:
            key_result, user_result = count_of(service, doctype), count_of(person, doctype)
            verdict = compare(key_result, user_result)
            verdicts.append(verdict)
            print(f"{doctype:28} {key_result[1]:>12} {user_result[1]:>16}   {verdict}")
        print("\n" + conclusion(verdicts))
    finally:
        person.session.close()


if __name__ == "__main__":
    main()