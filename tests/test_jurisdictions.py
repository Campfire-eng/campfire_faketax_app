from decimal import Decimal

from jurisdictions import DEFAULT_JURISDICTION, JURISDICTIONS, TAX_CODES, code_is_taxable, split_rate


def test_combined_rate_adds_state_and_local() -> None:
    assert JURISDICTIONS["NY"].combined_rate == Decimal("8.00")
    assert JURISDICTIONS["CO"].combined_rate == Decimal("7.80")


def test_every_taxing_state_has_a_state_county_and_supplemental_rate() -> None:
    ca = JURISDICTIONS["CA"]
    assert (ca.state_rate, ca.county_rate, ca.supplemental_rate) == (Decimal("6.00"), Decimal("0.25"), Decimal("1.00"))
    assert all(j.county_rate > 0 and j.supplemental_rate > 0 for j in JURISDICTIONS.values() if j.has_sales_tax)
    assert DEFAULT_JURISDICTION.combined_rate == Decimal("5.00")


def test_split_rate_keeps_the_jurisdictions_proportions_and_the_total() -> None:
    split = split_rate(total=Decimal("9.50"), like=JURISDICTIONS["CA"])
    assert (split.state, split.county, split.supplemental) == (Decimal("7.8621"), Decimal("0.3276"), Decimal("1.3103"))
    assert split.total == Decimal("9.50")
    # A state with no sales tax of its own borrows the default split for an overridden rate.
    assert split_rate(total=Decimal("5.00"), like=JURISDICTIONS["OR"]).county == Decimal("1.00")


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
