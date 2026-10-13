from decimal import Decimal

from jurisdictions import JURISDICTIONS, TAX_CODES, code_is_taxable


def test_combined_rate_adds_state_and_local() -> None:
    assert JURISDICTIONS["NY"].combined_rate == Decimal("8.00")
    assert JURISDICTIONS["CO"].combined_rate == Decimal("7.80")


def test_states_without_sales_tax() -> None:
    assert not JURISDICTIONS["OR"].has_sales_tax
    assert JURISDICTIONS["CA"].has_sales_tax


def test_tax_codes_follow_each_states_rules() -> None:
    assert set(TAX_CODES) == {"TAXABLE", "SAAS", "SERVICE", "EXEMPT"}
    assert code_is_taxable(code="SAAS", jurisdiction=JURISDICTIONS["TX"])
    assert not code_is_taxable(code="SAAS", jurisdiction=JURISDICTIONS["CA"])
    assert code_is_taxable(code="SERVICE", jurisdiction=JURISDICTIONS["WA"])
    assert not code_is_taxable(code="SERVICE", jurisdiction=JURISDICTIONS["NY"])
    assert not code_is_taxable(code="EXEMPT", jurisdiction=JURISDICTIONS["TX"])
    assert code_is_taxable(code=None, jurisdiction=JURISDICTIONS["CA"])
