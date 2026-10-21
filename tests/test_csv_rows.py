import csv
from decimal import Decimal
from pathlib import Path

import pytest

from loan_engine.csv_rows import (
    CsvFormatError,
    RowError,
    normalise_header,
    parse_row,
)

HEADER = ["user_id", "name", "email", "monthly_income", "credit_score", "employment_status", "age"]
GOOD = {
    "user_id": "b35de23e-755f-4c6a-81d8-f7dd99ce53ef",
    "name": "Dhruv Iyengar",
    "email": "Dhruv.Iyengar@Example.com",
    "monthly_income": "131000",
    "credit_score": "725",
    "employment_status": "Self-Employed",
    "age": "37",
}


def parse(**overrides):
    row = {**GOOD, **overrides}
    return parse_row(row, normalise_header(list(row)))


def test_valid_row_is_normalised():
    row = parse()
    assert row.email == "dhruv.iyengar@example.com"
    assert row.monthly_income == Decimal("131000.00")
    assert row.credit_score == 725
    assert row.employment_status == "Self-Employed"


@pytest.mark.parametrize("raw, expected", [
    ("salaried", "Salaried"),
    ("Self employed", "Self-Employed"),
    ("BUSINESS", "Business"),
])
def test_employment_aliases(raw, expected):
    assert parse(employment_status=raw).employment_status == expected


@pytest.mark.parametrize("income", ["45,000", "₹45,000", " 45000 "])
def test_income_formats(income):
    assert parse(monthly_income=income).monthly_income == Decimal("45000.00")


@pytest.mark.parametrize("field, value", [
    ("user_id", "not-a-uuid"),
    ("email", "no-at-sign"),
    ("monthly_income", "lots"),
    ("monthly_income", "-5"),
    ("credit_score", "950"),
    ("credit_score", "700.5"),
    ("age", "17"),
    ("employment_status", "Astronaut"),
    ("age", "NaN"),
])
def test_invalid_rows_are_rejected(field, value):
    with pytest.raises(RowError):
        parse(**{field: value})


def test_header_is_case_and_space_insensitive():
    mapping = normalise_header(["User_ID", " Email", "Monthly Income", "credit_score",
                                "Employment_Status", "AGE"])
    assert set(mapping.values()) >= {"user_id", "email", "monthly_income", "age"}


def test_missing_column_is_a_file_error():
    with pytest.raises(CsvFormatError, match="credit_score"):
        normalise_header(["user_id", "email", "monthly_income", "employment_status", "age"])


def test_sample_file_is_fully_valid():
    path = Path(__file__).parents[1] / "data" / "sample_users.csv"
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        header_map = normalise_header(reader.fieldnames)
        rows = [parse_row(raw, header_map) for raw in reader]
    assert len(rows) == 10_000
