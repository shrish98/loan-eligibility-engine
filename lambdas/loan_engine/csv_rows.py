"""Parse and validate rows of the user CSV.

Pure Python with no AWS or database imports, so it can be unit-tested on its own
(see tests/test_csv_rows.py). The ingest Lambda feeds it one CSV row at a time
while streaming the file from S3, so memory use stays flat for any file size.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

REQUIRED_COLUMNS = (
    "user_id",
    "email",
    "monthly_income",
    "credit_score",
    "employment_status",
    "age",
)
OPTIONAL_COLUMNS = ("name",)

# Every spelling we accept, mapped to the one value stored in the database.
# Loan products use the same three values in allowed_employment.
EMPLOYMENT_ALIASES = {
    "salaried": "Salaried",
    "salary": "Salaried",
    "employed": "Salaried",
    "self-employed": "Self-Employed",
    "self employed": "Self-Employed",
    "selfemployed": "Self-Employed",
    "self-employed professional": "Self-Employed",
    "business": "Business",
    "business owner": "Business",
    "self-employed business": "Business",
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class CsvFormatError(ValueError):
    """The file as a whole is unusable (for example, a required column is missing)."""


class RowError(ValueError):
    """A single row is invalid. The message is stored in the rejects report."""


@dataclass(frozen=True)
class UserRow:
    user_id: str
    name: str | None
    email: str
    monthly_income: Decimal
    credit_score: int
    employment_status: str
    age: int

    def as_copy_fields(self) -> list[str]:
        """Values in the column order of the staging table used by COPY."""
        return [
            self.user_id,
            self.name or "",
            self.email,
            str(self.monthly_income),
            str(self.credit_score),
            self.employment_status,
            str(self.age),
        ]


def normalise_header(fieldnames: list[str] | None) -> dict[str, str]:
    """Map each raw header to its canonical name ("Monthly Income " -> "monthly_income").

    Raises CsvFormatError if a required column is missing.
    """
    if not fieldnames:
        raise CsvFormatError("The file is empty or has no header row.")
    mapping = {raw: raw.strip().lower().replace(" ", "_") for raw in fieldnames if raw}
    missing = [col for col in REQUIRED_COLUMNS if col not in mapping.values()]
    if missing:
        raise CsvFormatError(f"Missing required column(s): {', '.join(missing)}")
    return mapping


def parse_row(raw: dict[str, str | None], header_map: dict[str, str]) -> UserRow:
    """Turn one raw CSV row into a validated UserRow, or raise RowError."""
    row = {header_map[k]: (v or "").strip() for k, v in raw.items() if k in header_map}

    user_id = _parse_uuid(row.get("user_id", ""))
    email = row.get("email", "").lower()
    if not _EMAIL_RE.match(email):
        raise RowError(f"invalid email {email!r}")

    income = _parse_number(row.get("monthly_income", ""), "monthly_income")
    if income < 0:
        raise RowError("monthly_income must not be negative")

    score = _parse_int(row.get("credit_score", ""), "credit_score")
    if not 300 <= score <= 900:
        raise RowError(f"credit_score {score} outside 300-900")

    age = _parse_int(row.get("age", ""), "age")
    if not 18 <= age <= 100:
        raise RowError(f"age {age} outside 18-100")

    employment = EMPLOYMENT_ALIASES.get(row.get("employment_status", "").lower())
    if employment is None:
        raise RowError(f"unknown employment_status {row.get('employment_status')!r}")

    return UserRow(
        user_id=user_id,
        name=row.get("name") or None,
        email=email,
        monthly_income=income,
        credit_score=score,
        employment_status=employment,
        age=age,
    )


def _parse_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise RowError(f"invalid user_id {value!r}") from None


def _parse_number(value: str, field: str) -> Decimal:
    # Accept "45000", "45,000", "₹45,000" and "45000.50".
    cleaned = value.replace(",", "").replace("₹", "").replace("Rs.", "").strip()
    try:
        number = Decimal(cleaned)
    except InvalidOperation:
        raise RowError(f"{field} is not a number: {value!r}") from None
    if not number.is_finite():
        raise RowError(f"{field} is not a number: {value!r}")
    return number.quantize(Decimal("0.01"))


def _parse_int(value: str, field: str) -> int:
    number = _parse_number(value, field)
    if number != number.to_integral_value():
        raise RowError(f"{field} must be a whole number: {value!r}")
    return int(number)
