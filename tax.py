"""FakeTax's made-up sales tax rules. Pure functions over the invoice JSON Campfire returns."""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

STATE_RATES: dict[str, Decimal] = {
    "CA": Decimal("7.25"),
    "NY": Decimal("8.00"),
    "TX": Decimal("6.25"),
    "WA": Decimal("6.50"),
}
STATE_NAMES: dict[str, str] = {"CALIFORNIA": "CA", "NEW YORK": "NY", "TEXAS": "TX", "WASHINGTON": "WA"}
CENTS: Decimal = Decimal("0.01")

# Fields FakeTax's own write changes; an update touching only these is our echo, not a user edit.
SELF_WRITTEN_FIELDS: frozenset[str] = frozenset(
    {"tax", "tax_description", "total_amount", "last_modified_at", "updated_at"}
)

# Line fields sent back on PATCH. Campfire replaces every line on a lines write, so anything
# the line carries that is not listed here would be lost.
WRITABLE_LINE_FIELDS: tuple[str, ...] = (
    "product",
    "product_bundle",
    "service_date",
    "description",
    "charge_type",
    "quantity",
    "rate",
    "amount",
    "discount",
    "discount_percentage",
    "department",
    "withholding_tax_rate",
    "entity_use_code",
)

_ZIP_STATE_RE = re.compile(r"\b([A-Z]{2})\s+\d{5}(?:-\d{4})?\b")
_COMMA_STATE_RE = re.compile(r",\s*([A-Za-z]{2})\b")


@dataclass(frozen=True)
class LineTax:
    line_id: int | None
    description: str
    base: Decimal
    tax: Decimal
    current_tax: Decimal


@dataclass(frozen=True)
class TaxResult:
    state: str | None
    rate: Decimal | None
    description: str
    lines: list[LineTax]

    @property
    def needs_rate(self) -> bool:
        return self.rate is None

    @property
    def total_tax(self) -> Decimal:
        return sum((line.tax for line in self.lines), Decimal("0"))

    @property
    def already_applied(self) -> bool:
        return all(line.tax == line.current_tax for line in self.lines)


def to_decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal("0")


def parse_rate(value: str | None) -> Decimal | None:
    if value is None or not value.strip():
        return None
    try:
        rate: Decimal = Decimal(value.strip())
    except InvalidOperation:
        return None
    return rate if Decimal("0") <= rate <= Decimal("100") else None


def state_from_address(address: str | None) -> str | None:
    if not address:
        return None
    upper: str = address.upper()
    for name, code in STATE_NAMES.items():
        if re.search(rf"\b{name}\b", upper):
            return code
    match = _ZIP_STATE_RE.search(upper) or _COMMA_STATE_RE.search(upper)
    return match.group(1).upper() if match else None


def shipping_state(invoice: Mapping[str, Any]) -> str | None:
    """Campfire stores the ship-to address as free text, so the state is parsed out of it."""
    return state_from_address(invoice.get("shipping_address")) or state_from_address(invoice.get("location_of_sale"))


def resolve_rate(*, state: str | None, default_rate: str | None) -> Decimal | None:
    if state in STATE_RATES:
        return STATE_RATES[state]
    return parse_rate(default_rate)


def tax_description(*, state: str | None, rate: Decimal) -> str:
    return f"FakeTax {state} {rate:.2f}%" if state else f"FakeTax {rate:.2f}%"


def line_tax(*, amount: Decimal, discount: Decimal, rate: Decimal) -> Decimal:
    return ((amount - discount) * rate / Decimal("100")).quantize(CENTS, rounding=ROUND_HALF_UP)


def compute_invoice_tax(
    *, invoice: Mapping[str, Any], default_rate: str | None, rate_override: Decimal | None = None
) -> TaxResult:
    state: str | None = shipping_state(invoice)
    rate: Decimal | None = rate_override if rate_override is not None else resolve_rate(state=state, default_rate=default_rate)
    lines: list[LineTax] = []
    for line in invoice.get("lines") or []:
        amount: Decimal = to_decimal(line.get("amount"))
        discount: Decimal = to_decimal(line.get("discount"))
        lines.append(
            LineTax(
                line_id=line.get("id"),
                description=line.get("description") or line.get("product_name") or "",
                base=amount - discount,
                tax=line_tax(amount=amount, discount=discount, rate=rate) if rate is not None else Decimal("0"),
                current_tax=to_decimal(line.get("tax")),
            )
        )
    return TaxResult(
        state=state,
        rate=rate,
        description=tax_description(state=state, rate=rate) if rate is not None else "",
        lines=lines,
    )


def lines_payload(*, invoice: Mapping[str, Any], result: TaxResult) -> list[dict[str, Any]]:
    """The full ``lines`` list for PATCH, each line as it was plus FakeTax's tax."""
    payload: list[dict[str, Any]] = []
    for line, computed in zip(invoice.get("lines") or [], result.lines, strict=True):
        item: dict[str, Any] = {field: line[field] for field in WRITABLE_LINE_FIELDS if field in line}
        item.update({key: value for key, value in line.items() if key.startswith("custom_field_")})
        item["tags"] = [{"id": tag["id"]} if isinstance(tag, dict) else tag for tag in line.get("tags") or []]
        item["tax"] = str(computed.tax)
        item["tax_description"] = result.description
        payload.append(item)
    return payload


def is_self_echo(changed_fields: Iterable[str]) -> bool:
    """
    True when an update only touched fields FakeTax writes itself. An empty list says nothing
    about what changed, so it is not treated as an echo; the recompute check catches those.
    """
    fields: set[str] = set(changed_fields)
    return bool(fields) and fields <= SELF_WRITTEN_FIELDS
