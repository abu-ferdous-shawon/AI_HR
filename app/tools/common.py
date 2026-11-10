import datetime

from app.core.security import ToolInputError


def to_int(value, label: str, low: int, high: int) -> int | None:
    """Models sometimes send 2026 as 2026.0 or '2026'. Accept those, reject nonsense."""
    if value is None or value == "":
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        raise ToolInputError(f"{label} must be a number.") from None
    if not low <= number <= high:
        raise ToolInputError(f"{label} must be between {low} and {high}.")
    return number


def parse_date(value, label: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        raise ToolInputError(f"{label} must be a real date written as YYYY-MM-DD.") from None


def resolve_range(year, from_date, to_date, max_days: int = 366,
                  default: str = "year") -> tuple[datetime.date, datetime.date]:
    """Turn year / from_date / to_date into a checked (start, end) pair."""
    today = datetime.date.today()
    y = to_int(year, "year", 2000, 2100)
    if from_date or to_date:
        end = parse_date(to_date, "to_date") if to_date else today
        start = (parse_date(from_date, "from_date") if from_date
                 else end - datetime.timedelta(days=29))
    elif y:
        start, end = datetime.date(y, 1, 1), datetime.date(y, 12, 31)
    elif default == "30days":
        start, end = today - datetime.timedelta(days=29), today
    else:
        start, end = datetime.date(today.year, 1, 1), datetime.date(today.year, 12, 31)

    if start > end:
        raise ToolInputError("from_date must not be after to_date.")
    if (end - start).days + 1 > max_days:
        raise ToolInputError(f"Please ask for at most {max_days} days at a time.")
    return start, end