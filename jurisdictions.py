"""
FakeTax's seeded tax tables: US state jurisdictions with a state rate, an average county rate and a
supplemental (special district) rate, which kinds of sales each state taxes, and the product tax codes a line can carry. The numbers
are illustrative, not advice.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

RATE_PLACES: Decimal = Decimal("0.0001")


@dataclass(frozen=True)
class Jurisdiction:
    code: str
    name: str
    state_rate: Decimal
    county_rate: Decimal
    supplemental_rate: Decimal
    taxes_services: bool
    taxes_saas: bool
    taxes_digital: bool
    taxes_shipping: bool

    @property
    def local_rate(self) -> Decimal:
        return self.county_rate + self.supplemental_rate

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
    # always | never | services | saas | digital | shipping | groceries
    rule: str

    @property
    def when(self) -> str:
        return RULE_LABELS[self.rule]


RULE_LABELS: dict[str, str] = {
    "always": "Wherever you have nexus",
    "never": "Never",
    "services": "States that tax services",
    "saas": "States that tax SaaS",
    "digital": "States that tax digital goods",
    "shipping": "States that tax delivery charges",
    "groceries": "States that tax groceries (none seeded)",
}


def _j(
    code: str, name: str, state: str, county: str, supplemental: str, services: bool, saas: bool, digital: bool, shipping: bool
) -> Jurisdiction:
    return Jurisdiction(
        code=code,
        name=name,
        state_rate=Decimal(state),
        county_rate=Decimal(county),
        supplemental_rate=Decimal(supplemental),
        taxes_services=services,
        taxes_saas=saas,
        taxes_digital=digital,
        taxes_shipping=shipping,
    )


JURISDICTIONS: dict[str, Jurisdiction] = {
    j.code: j
    for j in (
        #   code  name              state    county  suppl.  services saas   digital shipping
        _j("AZ", "Arizona", "5.60", "0.70", "2.10", False, True, True, False),
        _j("CA", "California", "6.00", "0.25", "1.00", False, False, False, False),
        _j("CO", "Colorado", "2.90", "1.00", "3.90", False, False, True, False),
        _j("DE", "Delaware", "0.00", "0.00", "0.00", False, False, False, False),
        _j("FL", "Florida", "6.00", "0.50", "0.50", False, False, False, False),
        _j("GA", "Georgia", "4.00", "3.00", "0.35", False, False, False, True),
        _j("IL", "Illinois", "6.25", "1.75", "0.85", False, False, False, False),
        _j("MA", "Massachusetts", "5.00", "0.75", "0.50", False, True, True, False),
        _j("MT", "Montana", "0.00", "0.00", "0.00", False, False, False, False),
        _j("NH", "New Hampshire", "0.00", "0.00", "0.00", False, False, False, False),
        _j("NJ", "New Jersey", "5.625", "0.50", "0.50", False, False, True, True),
        _j("NY", "New York", "4.00", "3.625", "0.375", False, True, True, True),
        _j("OH", "Ohio", "5.75", "1.25", "0.25", True, True, True, True),
        _j("OR", "Oregon", "0.00", "0.00", "0.00", False, False, False, False),
        _j("PA", "Pennsylvania", "6.00", "0.25", "0.10", False, True, True, True),
        _j("TX", "Texas", "5.25", "0.50", "0.50", True, True, True, True),
        _j("WA", "Washington", "5.00", "1.00", "0.50", True, True, True, True),
    )
}

# How a rate is split when the ship-to state is not in the table (the install default rate).
DEFAULT_JURISDICTION: Jurisdiction = _j("XX", "Default", "3.50", "1.00", "0.50", True, True, True, True)


@dataclass(frozen=True)
class TaxComponents:
    """One combined rate as the three rates Campfire books separately, in percent."""

    state: Decimal
    county: Decimal
    supplemental: Decimal

    @property
    def total(self) -> Decimal:
        return self.state + self.county + self.supplemental


def split_rate(*, total: Decimal, like: Jurisdiction) -> TaxComponents:
    """``total`` split the way ``like`` splits its own rate; the state share absorbs rounding."""
    if total == like.combined_rate:
        return TaxComponents(state=like.state_rate, county=like.county_rate, supplemental=like.supplemental_rate)
    basis: Jurisdiction = like if like.has_sales_tax else DEFAULT_JURISDICTION
    county: Decimal = (total * basis.county_rate / basis.combined_rate).quantize(RATE_PLACES, rounding=ROUND_HALF_UP)
    supplemental: Decimal = (total * basis.supplemental_rate / basis.combined_rate).quantize(
        RATE_PLACES, rounding=ROUND_HALF_UP
    )
    return TaxComponents(state=total - county - supplemental, county=county, supplemental=supplemental)


STATE_NAMES: dict[str, str] = {j.name.upper(): j.code for j in JURISDICTIONS.values()}


def _c(code: str, label: str, description: str, rule: str) -> TaxCode:
    return TaxCode(code=code, label=label, description=description, rule=rule)


# FakeTax product tax codes: FT-<family>-<number>, the shape a real engine's catalog takes.
TAX_CODES: dict[str, TaxCode] = {
    code.code: code
    for code in (
        _c("FT-GOODS-100", "Tangible goods", "Physical products shipped or handed over.", "always"),
        _c("FT-SOFT-200", "Software as a service", "Hosted software billed by subscription or usage.", "saas"),
        _c("FT-SOFT-210", "Downloaded software and digital goods", "Licenses, e-books, media delivered electronically.", "digital"),
        _c("FT-SVC-300", "Professional services", "Consulting, implementation, support hours.", "services"),
        _c("FT-SVC-310", "Installation and repair", "Labor on tangible goods; taxed like services.", "services"),
        _c("FT-SHIP-400", "Shipping and handling", "Separately stated delivery charges.", "shipping"),
        _c("FT-FOOD-500", "Groceries", "Unprepared food for home consumption.", "groceries"),
        _c("FT-MED-510", "Medical devices and prescriptions", "Exempt medical items.", "never"),
        _c("FT-EXEMPT-900", "Resale", "Buyer holds a resale certificate.", "never"),
        _c("FT-EXEMPT-910", "Exempt organization", "Government, education or non-profit purchaser.", "never"),
    )
}
DEFAULT_TAX_CODE: str = "FT-GOODS-100"

# The first-generation names, still accepted from older custom field values and settings.
LEGACY_CODES: dict[str, str] = {
    "TAXABLE": "FT-GOODS-100",
    "SAAS": "FT-SOFT-200",
    "SERVICE": "FT-SVC-300",
    "EXEMPT": "FT-EXEMPT-900",
}


def normalize_tax_code(code: str | None) -> str:
    """A catalog code, resolving legacy names; unknown values fall back to the default."""
    raw: str = (code or "").strip().upper()
    canonical: str = LEGACY_CODES.get(raw, raw)
    return canonical if canonical in TAX_CODES else DEFAULT_TAX_CODE


def code_is_taxable(*, code: str | None, jurisdiction: Jurisdiction) -> bool:
    """Whether a line with this tax code is taxed in the jurisdiction."""
    rule: str = TAX_CODES[normalize_tax_code(code)].rule
    if rule == "always":
        return True
    if rule == "never" or rule == "groceries":
        return False
    return {
        "services": jurisdiction.taxes_services,
        "saas": jurisdiction.taxes_saas,
        "digital": jurisdiction.taxes_digital,
        "shipping": jurisdiction.taxes_shipping,
    }[rule]


# What a product or description usually means, when a line carries no code of its own.
CLASSIFIER_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("FT-SHIP-400", ("shipping", "freight", "delivery", "handling")),
    ("FT-SVC-310", ("install", "repair", "maintenance labor", "onsite")),
    ("FT-SVC-300", ("consult", "service", "support", "implementation", "training", "hours", "professional", "onboarding")),
    ("FT-SOFT-210", ("download", "license", "licence", "e-book", "ebook", "digital")),
    ("FT-SOFT-200", ("subscription", "saas", "software", "platform", "cloud", "seat", "automation", "api")),
    ("FT-FOOD-500", ("grocer", "produce", "food")),
    ("FT-MED-510", ("medical", "prescription", "rx")),
)


def classify_line_text(text: str) -> str | None:
    """A code suggested from a product name or description; None when nothing matches."""
    lowered: str = text.lower()
    for code, keywords in CLASSIFIER_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return code
    return None
