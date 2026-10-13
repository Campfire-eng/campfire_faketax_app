"""FakeTax's made-up sales tax rules. Pure functions over the invoice JSON Campfire returns."""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from jurisdictions import (
    JURISDICTIONS,
    STATE_NAMES,
    TAX_CODES,
    Jurisdiction,
    classify_line_text,
    code_is_taxable,
    normalize_tax_code,
)
from store import WorkspaceTax

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


# Credit memo lines carry fewer fields than invoice lines; the same replace-all rule applies.
WRITABLE_CREDIT_MEMO_LINE_FIELDS: tuple[str, ...] = ("product", "account", "description", "amount", "department")


@dataclass(frozen=True)
class LineTax:
    line_id: int | None
    description: str
    base: Decimal
    tax: Decimal
    current_tax: Decimal
    tax_code: str = "FT-GOODS-100"
    taxable: bool = True
    # True when the line carried no code and FakeTax chose one; the write puts it on the line.
    code_assigned: bool = False


@dataclass(frozen=True)
class TaxResult:
    state: str | None
    rate: Decimal | None
    description: str
    lines: list[LineTax]
    # False when the workspace has no nexus in the state: the rate is then 0, not unknown.
    nexus: bool = True

    @property
    def needs_rate(self) -> bool:
        return self.rate is None

    @property
    def total_tax(self) -> Decimal:
        return sum((line.tax for line in self.lines), Decimal("0"))

    @property
    def already_applied(self) -> bool:
        return all(line.tax == line.current_tax and not line.code_assigned for line in self.lines)


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


def jurisdiction_rate(*, state: str, workspace: WorkspaceTax | None) -> Decimal:
    """The workspace's override for the state, else the seeded combined rate."""
    override: str | None = workspace.overrides.get(state) if workspace else None
    parsed: Decimal | None = parse_rate(override)
    if parsed is not None:
        return parsed
    return JURISDICTIONS[state].combined_rate


def has_nexus(*, state: str, workspace: WorkspaceTax | None) -> bool:
    """Before onboarding FakeTax taxes everywhere, as a demo should; after it, only where told."""
    return workspace is None or state in workspace.nexus


def resolve_rate(*, state: str | None, default_rate: str | None, workspace: WorkspaceTax | None = None) -> Decimal | None:
    if state in JURISDICTIONS:
        if not has_nexus(state=state, workspace=workspace):
            return Decimal("0")
        return jurisdiction_rate(state=state, workspace=workspace)
    return parse_rate(default_rate)


def tax_description(*, state: str | None, rate: Decimal, nexus: bool = True) -> str:
    if state and not nexus:
        return f"FakeTax {state} no nexus"
    if state in JURISDICTIONS and not JURISDICTIONS[state].has_sales_tax and rate == 0:
        return f"FakeTax {state} no sales tax"
    return f"FakeTax {state} {rate:.2f}%" if state else f"FakeTax {rate:.2f}%"


TAX_CODE_FIELD: str = "tax_code"


def tax_code_field_for(*, line: Mapping[str, Any], workspace: WorkspaceTax | None) -> str | None:
    """
    The custom field carrying the tax code: what onboarding recorded, or the conventional name
    when Campfire already sends it on the line, so a workspace that skipped that step still works.
    """
    if workspace is not None and workspace.tax_code_field:
        return workspace.tax_code_field
    return TAX_CODE_FIELD if f"custom_field_{TAX_CODE_FIELD}" in line else None


def line_tax_code(*, line: Mapping[str, Any], workspace: WorkspaceTax | None) -> tuple[str, bool]:
    """
    The line's tax code and whether FakeTax assigned it. A code on the line wins; otherwise the
    product name and description are classified, and the workspace default is the last resort.
    """
    if workspace is None:
        return normalize_tax_code(None), False
    field: str | None = tax_code_field_for(line=line, workspace=workspace)
    value: Any = line.get(f"custom_field_{field}") if field else None
    if value:
        return normalize_tax_code(str(value)), False
    text: str = " ".join(str(line.get(key) or "") for key in ("product_name", "description"))
    suggested: str | None = classify_line_text(text)
    return normalize_tax_code(suggested or workspace.default_tax_code), True


def line_tax(*, amount: Decimal, discount: Decimal, rate: Decimal) -> Decimal:
    return ((amount - discount) * rate / Decimal("100")).quantize(CENTS, rounding=ROUND_HALF_UP)


def compute_lines_tax(
    *,
    lines_in: list[Mapping[str, Any]],
    state: str | None,
    default_rate: str | None,
    workspace: WorkspaceTax | None,
    rate_override: Decimal | None,
) -> TaxResult:
    nexus: bool = state is None or state not in JURISDICTIONS or has_nexus(state=state, workspace=workspace)
    rate: Decimal | None = (
        rate_override
        if rate_override is not None
        else resolve_rate(state=state, default_rate=default_rate, workspace=workspace)
    )
    jurisdiction: Jurisdiction | None = JURISDICTIONS.get(state or "")
    lines: list[LineTax] = []
    for line in lines_in:
        amount: Decimal = to_decimal(line.get("amount"))
        discount: Decimal = to_decimal(line.get("discount"))
        code, assigned = line_tax_code(line=line, workspace=workspace)
        taxable: bool = (
            code_is_taxable(code=code, jurisdiction=jurisdiction)
            if jurisdiction
            else TAX_CODES[code].rule != "never"
        )
        lines.append(
            LineTax(
                line_id=line.get("id"),
                description=line.get("description") or line.get("product_name") or "",
                base=amount - discount,
                tax=line_tax(amount=amount, discount=discount, rate=rate) if rate is not None and taxable else Decimal("0"),
                current_tax=to_decimal(line.get("tax")),
                tax_code=code,
                taxable=taxable,
                code_assigned=assigned,
            )
        )
    return TaxResult(
        state=state,
        rate=rate,
        description=tax_description(state=state, rate=rate, nexus=nexus) if rate is not None else "",
        lines=lines,
        nexus=nexus,
    )


def compute_invoice_tax(
    *,
    invoice: Mapping[str, Any],
    default_rate: str | None,
    rate_override: Decimal | None = None,
    workspace: WorkspaceTax | None = None,
) -> TaxResult:
    return compute_lines_tax(
        lines_in=list(invoice.get("lines") or []),
        state=shipping_state(invoice),
        default_rate=default_rate,
        workspace=workspace,
        rate_override=rate_override,
    )


def compute_credit_memo_tax(
    *, memo: Mapping[str, Any], state: str | None, default_rate: str | None, workspace: WorkspaceTax | None
) -> TaxResult:
    """A credit memo has no ship-to address; the caller resolves the state from its client."""
    return compute_lines_tax(
        lines_in=list(memo.get("lines") or []),
        state=state,
        default_rate=default_rate,
        workspace=workspace,
        rate_override=None,
    )


def _line_payload(
    *, line: Mapping[str, Any], computed: LineTax, result: TaxResult, fields: tuple[str, ...], tax_code_field: str | None
) -> dict[str, Any]:
    item: dict[str, Any] = {field: line[field] for field in fields if field in line}
    item.update({key: value for key, value in line.items() if key.startswith("custom_field_")})
    item["tags"] = [{"id": tag["id"]} if isinstance(tag, dict) else tag for tag in line.get("tags") or []]
    item["tax"] = str(computed.tax)
    item["tax_description"] = result.description
    field: str | None = tax_code_field or (TAX_CODE_FIELD if f"custom_field_{TAX_CODE_FIELD}" in line else None)
    if field and computed.code_assigned:
        item[f"custom_field_{field}"] = computed.tax_code
    return item


def lines_payload(
    *, invoice: Mapping[str, Any], result: TaxResult, tax_code_field: str | None = None
) -> list[dict[str, Any]]:
    """The full ``lines`` list for PATCH: each line as it was, plus FakeTax's tax and any code it assigned."""
    return [
        _line_payload(line=line, computed=computed, result=result, fields=WRITABLE_LINE_FIELDS, tax_code_field=tax_code_field)
        for line, computed in zip(invoice.get("lines") or [], result.lines, strict=True)
    ]


def credit_memo_lines_payload(
    *, memo: Mapping[str, Any], result: TaxResult, tax_code_field: str | None = None
) -> list[dict[str, Any]]:
    return [
        _line_payload(
            line=line, computed=computed, result=result, fields=WRITABLE_CREDIT_MEMO_LINE_FIELDS, tax_code_field=tax_code_field
        )
        for line, computed in zip(memo.get("lines") or [], result.lines, strict=True)
    ]


def vendor_address(vendor: Mapping[str, Any]) -> str:
    """Campfire's vendor record keeps the address in pieces; join them so the state parser can read it."""
    parts: list[str] = [
        str(vendor.get(key) or "")
        for key in ("address_street_1", "address_street_2", "city", "state", "zip_code", "country")
    ]
    city_state: str = ", ".join(part for part in (parts[2], f"{parts[3]} {parts[4]}".strip()) if part)
    return "\n".join(part for part in (parts[0], parts[1], city_state, parts[5]) if part)


def is_self_echo(changed_fields: Iterable[str]) -> bool:
    """
    True when an update only touched fields FakeTax writes itself. An empty list says nothing
    about what changed, so it is not treated as an echo; the recompute check catches those.
    """
    fields: set[str] = set(changed_fields)
    return bool(fields) and fields <= SELF_WRITTEN_FIELDS
