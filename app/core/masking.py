import re
from dataclasses import dataclass, field
from typing import Any
import math

# Fields whose VALUES are never shown to the AI model. Add your own custom
# field names here (see the check script in Step 9).
CONFIDENTIAL_FIELDS = {
    "date_of_birth", "personal_email", "company_email", "prefered_email",
    "prefered_contact_email", "cell_number", "emergency_phone_number",
    "person_to_be_contacted", "relation", "current_address", "permanent_address",
    "marital_status", "family_background", "blood_group", "health_details", "bio",
    "ctc", "salary_mode", "salary_currency", "bank_name", "bank_ac_no", "iban",
    "passport_number", "valid_upto", "date_of_issue", "place_of_issue",
    "health_insurance_provider", "health_insurance_no",
}

# Safety net for custom fields: any field name containing one of these
# words (split at underscores) is treated as confidential too.
CONFIDENTIAL_WORDS = {
    "phone", "mobile", "cell", "email", "address", "birth", "dob", "bank",
    "salary", "ctc", "passport", "iban", "insurance", "nid", "national", "tax",
    "tin", "ssn", "blood", "marital", "health", "religion", "emergency",
    "pension", "payroll", "wage", "visa",
    # payroll amounts
    "pay", "gross", "deduction", "deductions", "earning", "earnings",
    "amount", "bonus", "loan",
        "tin", "ssn", "blood", "marital", "health", "religion", "emergency", "contact",
}
# Matches [[MASK_3]] and sloppy copies like [[ mask_3 ]].
TOKEN_RE = re.compile(r"\[\[\s*MASK[_\s-]?(\d+)\s*\]\]", re.IGNORECASE)
# Bangla digits ০-৯ -> 0-9, one character each, so positions line up with the original.
BANGLA_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

# Things a person might TYPE that must not reach the model.
TYPED_SECRET_RE = re.compile(
    r"""
      [A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+            # email address
    | (?<!\d)(?:\+?880[\s-]?|0)1[3-9](?:[\s-]?\d){8}(?!\d)           # Bangladesh mobile number
    | (?<!\d)\d{9,}(?!\d)                                            # NID, account, card or passport number
    """,
    re.VERBOSE,
)


# Names added at run time from the registry profile (see register_confidential).
EXTRA_CONFIDENTIAL: set[str] = set()


def register_confidential(names) -> None:
    """Treat these field names as confidential too (called with a profile's names)."""
    EXTRA_CONFIDENTIAL.update(str(n).lower() for n in names)


def is_confidential(field_name: str) -> bool:
    name = str(field_name).lower()
    return (name in CONFIDENTIAL_FIELDS or name in EXTRA_CONFIDENTIAL
            or any(word in CONFIDENTIAL_WORDS for word in name.split("_")))

def format_number(value) -> str:
    """Plain text for a calculated number: 45000.0 -> '45000', 12.5 -> '12.5'."""
    if isinstance(value, int):
        return str(value)
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"

@dataclass
class Vault:
    """One conversation's secret list: placeholder <-> real value.

    It lives only in the server's memory and is never sent to the model.
    """
    by_value: dict[str, str] = field(default_factory=dict)
    by_token: dict[str, str] = field(default_factory=dict)

    def _token_for(self, real: str) -> str:
        token = self.by_value.get(real)
        if token is None:  # same value -> same placeholder, every time
            token = f"[[MASK_{len(self.by_token) + 1}]]"
            self.by_value[real] = token
            self.by_token[token] = real
        return token

    def mask(self, data: Any, _inside: bool = False) -> Any:
        """Return a copy of data with confidential values swapped for placeholders."""
        if isinstance(data, dict):
            return {k: self.mask(v, _inside or is_confidential(k)) for k, v in data.items()}
        if isinstance(data, list):
            return [self.mask(v, _inside) for v in data]
        if _inside and isinstance(data, (str, int, float)) and str(data).strip():
            return self._token_for(str(data))
        return data  # empty values and everything else stay as they are

    def mask_typed(self, text: str) -> str:
        """Mask phone numbers, emails and long ID/account numbers in text a person typed.

        Bangla digits are recognised too. The ORIGINAL characters are what gets
        restored later, so a number typed in Bangla digits comes back in Bangla digits.
        """
        normalised = text.translate(BANGLA_DIGITS)   # same length as text
        pieces, last = [], 0
        for match in TYPED_SECRET_RE.finditer(normalised):
            pieces.append(text[last:match.start()])
            pieces.append(self._token_for(text[match.start():match.end()]))
            last = match.end()
        pieces.append(text[last:])
        return "".join(pieces)

    def unmask(self, text: str) -> str:
        """Put real values back into text. Unknown placeholders are never shown raw."""
        def restore(match: re.Match) -> str:
            return self.by_token.get(f"[[MASK_{match.group(1)}]]", "(not available)")
        # One pass only, so a restored value is never scanned for placeholders again.
        return TOKEN_RE.sub(restore, text)

    def unknown_placeholders(self, text: str) -> list[str]:
        """Placeholders in text that do not exist in this vault (the model made them up)."""
        return [m.group(0) for m in TOKEN_RE.finditer(text)
                if f"[[MASK_{m.group(1)}]]" not in self.by_token]

    def hide(self, value) -> str:
        """Placeholder for a number that was calculated from confidential inputs."""
        return self._token_for(format_number(value))

    def reveal_numbers(self, expression: str) -> tuple[str, bool]:
        """Put the real numbers back into a calculation. Returns (expression, used_a_secret)."""
        used = False

        def fill(match: re.Match) -> str:
            nonlocal used
            real = self.by_token.get(f"[[MASK_{match.group(1)}]]")
            if real is None:
                raise ValueError("That calculation uses a placeholder that does not exist.")
            cleaned = real.replace(",", "").strip().translate(BANGLA_DIGITS)
            try:
                number = float(cleaned)
            except ValueError:
                raise ValueError("A placeholder in the calculation is not a number.") from None
            if not math.isfinite(number):
                raise ValueError("A placeholder in the calculation is not a number.")
            used = True
            return f"({cleaned})"

        return TOKEN_RE.sub(fill, expression), used