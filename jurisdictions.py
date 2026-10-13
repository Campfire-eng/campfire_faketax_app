"""
FakeTax's seeded tax tables: US state jurisdictions with a state rate and an average local add-on,
which kinds of sales each state taxes, and the product tax codes a line can carry. The numbers
are illustrative, not advice.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Jurisdiction:
    code: str
    name: str
    state_rate: Decimal
    local_rate: Decimal
    taxes_services: bool
    taxes_saas: bool

    @property
    def combined_rate(self) -> Decimal:
        return self.state_rate + self.local_rate

    @property
    def has_sales_tax(self) -> bool:
        return self.combined_rate > 0


@dataclass(frozen=True)
class TaxCode:
    code: str
    label: str
    description: str


def _j(code: str, name: str, state: str, local: str, services: bool, saas: bool) -> Jurisdiction:
    return Jurisdiction(
        code=code,
        name=name,
        state_rate=Decimal(state),
        local_rate=Decimal(local),
        taxes_services=services,
        taxes_saas=saas,
    )


JURISDICTIONS: dict[str, Jurisdiction] = {
    j.code: j
    for j in (
        _j("AZ", "Arizona", "5.60", "2.80", False, True),
        _j("CA", "California", "7.25", "0.00", False, False),
        _j("CO", "Colorado", "2.90", "4.90", False, False),
        _j("DE", "Delaware", "0.00", "0.00", False, False),
        _j("FL", "Florida", "6.00", "1.00", False, False),
        _j("GA", "Georgia", "4.00", "3.35", False, False),
        _j("IL", "Illinois", "6.25", "2.60", False, False),
        _j("MA", "Massachusetts", "6.25", "0.00", False, True),
        _j("MT", "Montana", "0.00", "0.00", False, False),
        _j("NH", "New Hampshire", "0.00", "0.00", False, False),
        _j("NJ", "New Jersey", "6.625", "0.00", False, False),
        _j("NY", "New York", "4.00", "4.00", False, True),
        _j("OH", "Ohio", "5.75", "1.50", True, True),
        _j("OR", "Oregon", "0.00", "0.00", False, False),
        _j("PA", "Pennsylvania", "6.00", "0.35", False, True),
        _j("TX", "Texas", "6.25", "0.00", True, True),
        _j("WA", "Washington", "6.50", "0.00", True, True),
    )
}

STATE_NAMES: dict[str, str] = {j.name.upper(): j.code for j in JURISDICTIONS.values()}

TAX_CODES: dict[str, TaxCode] = {
    code.code: code
    for code in (
        TaxCode("TAXABLE", "Taxable goods", "Tangible goods; taxed wherever you have nexus."),
        TaxCode("SAAS", "Software / SaaS", "Taxed only in states that tax digital products."),
        TaxCode("SERVICE", "Professional services", "Taxed only in states that tax services."),
        TaxCode("EXEMPT", "Exempt", "Resale, non-profit or otherwise exempt; never taxed."),
    )
}
DEFAULT_TAX_CODE: str = "TAXABLE"


def code_is_taxable(*, code: str | None, jurisdiction: Jurisdiction) -> bool:
    """Whether a line with this tax code is taxed in the jurisdiction."""
    normalized: str = (code or DEFAULT_TAX_CODE).strip().upper()
    if normalized == "EXEMPT":
        return False
    if normalized == "SERVICE":
        return jurisdiction.taxes_services
    if normalized == "SAAS":
        return jurisdiction.taxes_saas
    return True
