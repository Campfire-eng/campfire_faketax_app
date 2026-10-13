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
    taxes_digital: bool
    taxes_shipping: bool

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


def _j(code: str, name: str, state: str, local: str, services: bool, saas: bool, digital: bool, shipping: bool) -> Jurisdiction:
    return Jurisdiction(
        code=code,
        name=name,
        state_rate=Decimal(state),
        local_rate=Decimal(local),
        taxes_services=services,
        taxes_saas=saas,
        taxes_digital=digital,
        taxes_shipping=shipping,
    )


JURISDICTIONS: dict[str, Jurisdiction] = {
    j.code: j
    for j in (
        #   code  name              state    local   services saas   digital shipping
        _j("AZ", "Arizona", "5.60", "2.80", False, True, True, False),
        _j("CA", "California", "7.25", "0.00", False, False, False, False),
        _j("CO", "Colorado", "2.90", "4.90", False, False, True, False),
        _j("DE", "Delaware", "0.00", "0.00", False, False, False, False),
        _j("FL", "Florida", "6.00", "1.00", False, False, False, False),
        _j("GA", "Georgia", "4.00", "3.35", False, False, False, True),
        _j("IL", "Illinois", "6.25", "2.60", False, False, False, False),
        _j("MA", "Massachusetts", "6.25", "0.00", False, True, True, False),
        _j("MT", "Montana", "0.00", "0.00", False, False, False, False),
        _j("NH", "New Hampshire", "0.00", "0.00", False, False, False, False),
        _j("NJ", "New Jersey", "6.625", "0.00", False, False, True, True),
        _j("NY", "New York", "4.00", "4.00", False, True, True, True),
        _j("OH", "Ohio", "5.75", "1.50", True, True, True, True),
        _j("OR", "Oregon", "0.00", "0.00", False, False, False, False),
        _j("PA", "Pennsylvania", "6.00", "0.35", False, True, True, True),
        _j("TX", "Texas", "6.25", "0.00", True, True, True, True),
        _j("WA", "Washington", "6.50", "0.00", True, True, True, True),
    )
}

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
