from decimal import Decimal

from jurisdictions import JURISDICTIONS, TAX_CODES, code_is_taxable


def test_combined_rate_adds_state_and_local() -> None:
    assert JURISDICTIONS["NY"].combined_rate == Decimal("8.00")
    assert JURISDICTIONS["CO"].combined_rate == Decimal("7.80")


def test_states_without_sales_tax() -> None:
    assert not JURISDICTIONS["OR"].has_sales_tax
    assert JURISDICTIONS["CA"].has_sales_tax


def test_tax_codes_follow_each_states_rules() -> None:
    assert len(TAX_CODES) == 10 and all(code.startswith("FT-") for code in TAX_CODES)
    assert code_is_taxable(code="FT-SOFT-200", jurisdiction=JURISDICTIONS["TX"])
    assert code_is_taxable(code="FT-SHIP-400", jurisdiction=JURISDICTIONS["NY"])
    assert not code_is_taxable(code="FT-SHIP-400", jurisdiction=JURISDICTIONS["CA"])
    assert not code_is_taxable(code="FT-FOOD-500", jurisdiction=JURISDICTIONS["TX"])
    assert code_is_taxable(code="FT-SOFT-210", jurisdiction=JURISDICTIONS["CO"])
    assert not code_is_taxable(code="FT-MED-510", jurisdiction=JURISDICTIONS["WA"])
    # First-generation names still resolve.
    assert code_is_taxable(code="SAAS", jurisdiction=JURISDICTIONS["TX"])
    assert not code_is_taxable(code="SAAS", jurisdiction=JURISDICTIONS["CA"])
    assert code_is_taxable(code="SERVICE", jurisdiction=JURISDICTIONS["WA"])
    assert not code_is_taxable(code="SERVICE", jurisdiction=JURISDICTIONS["NY"])
    assert not code_is_taxable(code="EXEMPT", jurisdiction=JURISDICTIONS["TX"])
    assert code_is_taxable(code=None, jurisdiction=JURISDICTIONS["CA"])
