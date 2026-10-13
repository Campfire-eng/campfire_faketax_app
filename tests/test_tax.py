from decimal import Decimal

from store import WorkspaceTax
from tax import (
    compute_credit_memo_tax,
    compute_invoice_tax,
    is_self_echo,
    line_tax,
    lines_payload,
    shipping_state,
    state_from_address,
)


def _invoice(*, address: str, lines: list[dict]) -> dict:
    return {"id": 7, "shipping_address": address, "lines": lines}


class TestLineTax:
    def test_rounds_half_up_to_cents(self) -> None:
        assert line_tax(amount=Decimal("10.10"), discount=Decimal("0"), rate=Decimal("7.25")) == Decimal("0.73")
        assert line_tax(amount=Decimal("0.70"), discount=Decimal("0"), rate=Decimal("7.50")) == Decimal("0.05")

    def test_taxes_amount_after_discount(self) -> None:
        assert line_tax(amount=Decimal("100"), discount=Decimal("20"), rate=Decimal("8.00")) == Decimal("6.40")


class TestShippingState:
    def test_reads_state_before_zip(self) -> None:
        assert state_from_address("1 Market St\nSan Francisco, CA 94105\nUSA") == "CA"

    def test_reads_full_state_name(self) -> None:
        assert state_from_address("Seattle, Washington") == "WA"

    def test_falls_back_to_location_of_sale(self) -> None:
        assert shipping_state({"shipping_address": "", "location_of_sale": "Austin, TX 78701"}) == "TX"

    def test_unknown_when_no_address(self) -> None:
        assert shipping_state({"shipping_address": None}) is None


class TestComputeInvoiceTax:
    def test_uses_state_table_rate_and_description(self) -> None:
        invoice = _invoice(address="Brooklyn, NY 11201", lines=[{"id": 1, "amount": "250.00", "discount": "0", "tax": "0"}])

        result = compute_invoice_tax(invoice=invoice, default_rate="5.00")

        assert result.rate == Decimal("8.00")
        assert result.description == "FakeTax NY 8.00%"
        assert result.lines[0].tax == Decimal("20.00")

    def test_uses_install_default_outside_table(self) -> None:
        invoice = _invoice(address="Boise, ID 83702", lines=[{"id": 1, "amount": 100, "tax": 0}])

        result = compute_invoice_tax(invoice=invoice, default_rate="5.00")

        assert result.rate == Decimal("5.00")
        assert result.description == "FakeTax ID 5.00%"

    def test_needs_rate_without_table_match_or_default(self) -> None:
        invoice = _invoice(address="Boise, ID 83702", lines=[{"id": 1, "amount": 100, "tax": 0}])

        result = compute_invoice_tax(invoice=invoice, default_rate="")

        assert result.needs_rate

    def test_override_wins(self) -> None:
        invoice = _invoice(address="San Jose, CA 95112", lines=[{"id": 1, "amount": 100, "tax": 0}])

        result = compute_invoice_tax(invoice=invoice, default_rate="5.00", rate_override=Decimal("1.5"))

        assert result.lines[0].tax == Decimal("1.50")

    def test_already_applied_when_stored_tax_matches(self) -> None:
        invoice = _invoice(address="San Jose, CA 95112", lines=[{"id": 1, "amount": "100.00", "tax": "7.25"}])

        assert compute_invoice_tax(invoice=invoice, default_rate="5.00").already_applied


class TestLinesPayload:
    def test_keeps_line_fields_and_sets_tax(self) -> None:
        invoice = _invoice(
            address="San Jose, CA 95112",
            lines=[
                {
                    "id": 1,
                    "product": 9,
                    "product_name": "Widget",
                    "amount": "100.00",
                    "quantity": "1",
                    "rate": "100.00",
                    "tags": [{"id": 3, "name": "West"}],
                    "custom_field_po": "A1",
                    "tax": "0",
                }
            ],
        )
        result = compute_invoice_tax(invoice=invoice, default_rate="5.00")

        payload = lines_payload(invoice=invoice, result=result)

        assert payload == [
            {
                "product": 9,
                "amount": "100.00",
                "quantity": "1",
                "rate": "100.00",
                "custom_field_po": "A1",
                "tags": [{"id": 3}],
                "tax": "7.25",
                "tax_description": "FakeTax CA 7.25%",
            }
        ]


class TestLoopGuard:
    def test_only_tax_fields_is_an_echo(self) -> None:
        assert is_self_echo(["tax", "total_amount", "last_modified_at"])

    def test_user_edit_is_not_an_echo(self) -> None:
        assert not is_self_echo(["tax", "shipping_address"])

    def test_empty_change_list_is_not_an_echo(self) -> None:
        assert not is_self_echo([])


def _workspace(**overrides) -> WorkspaceTax:
    values = {
        "customer_id": 42,
        "nexus": ["CA", "TX"],
        "overrides": {},
        "default_tax_code": "TAXABLE",
        "tax_code_field": "tax_code",
        "onboarded_at": 1.0,
    }
    values.update(overrides)
    return WorkspaceTax(**values)


class TestNexusAndTaxCodes:
    def test_no_tax_outside_nexus(self) -> None:
        invoice = _invoice(address="Albany, NY 12207", lines=[{"id": 1, "amount": 100, "tax": 0}])

        result = compute_invoice_tax(invoice=invoice, default_rate="5.00", workspace=_workspace())

        assert result.rate == Decimal("0") and not result.nexus
        assert result.description == "FakeTax NY no nexus"
        assert result.lines[0].tax == Decimal("0")

    def test_override_beats_the_table_inside_nexus(self) -> None:
        invoice = _invoice(address="Oakland, CA 94607", lines=[{"id": 1, "amount": 100, "tax": 0}])

        result = compute_invoice_tax(invoice=invoice, default_rate="", workspace=_workspace(overrides={"CA": "9.50"}))

        assert result.rate == Decimal("9.50") and result.lines[0].tax == Decimal("9.50")

    def test_line_tax_codes_decide_taxability(self) -> None:
        invoice = _invoice(
            address="Austin, TX 78701",
            lines=[
                {"id": 1, "amount": 100, "tax": 0, "custom_field_tax_code": "SAAS"},
                {"id": 2, "amount": 100, "tax": 0, "custom_field_tax_code": "EXEMPT"},
                {"id": 3, "amount": 100, "tax": 0},
            ],
        )

        result = compute_invoice_tax(invoice=invoice, default_rate="", workspace=_workspace(default_tax_code="SERVICE"))

        assert [line.tax_code for line in result.lines] == ["FT-SOFT-200", "FT-EXEMPT-900", "FT-SVC-300"]
        assert [line.taxable for line in result.lines] == [True, False, True]
        assert [line.tax for line in result.lines] == [Decimal("6.25"), Decimal("0"), Decimal("6.25")]

    def test_no_sales_tax_state_is_zero_not_unknown(self) -> None:
        invoice = _invoice(address="Portland, OR 97201", lines=[{"id": 1, "amount": 100, "tax": 0}])

        result = compute_invoice_tax(invoice=invoice, default_rate="", workspace=_workspace(nexus=["OR"]))

        assert not result.needs_rate and result.description == "FakeTax OR no sales tax"

    def test_credit_memo_uses_the_given_state(self) -> None:
        memo = {"id": 9, "lines": [{"id": 1, "amount": "40.00", "tax": "0"}]}

        result = compute_credit_memo_tax(memo=memo, state="CA", default_rate="", workspace=_workspace())

        assert result.lines[0].tax == Decimal("2.90") and result.description == "FakeTax CA 7.25%"


class TestCodeAssignment:
    def test_uncoded_lines_are_classified_and_written_back(self) -> None:
        invoice = _invoice(
            address="Austin, TX 78701",
            lines=[
                {"id": 1, "amount": 100, "tax": 0, "product_name": "Core Accounting + Revenue automation"},
                {"id": 2, "amount": 50, "tax": 0, "description": "Shipping"},
                {"id": 3, "amount": 10, "tax": 0, "custom_field_tax_code": "FT-EXEMPT-910"},
            ],
        )

        result = compute_invoice_tax(invoice=invoice, default_rate="", workspace=_workspace(nexus=["TX"]))
        payload = lines_payload(invoice=invoice, result=result, tax_code_field="tax_code")

        assert [(line.tax_code, line.code_assigned) for line in result.lines] == [
            ("FT-SOFT-200", True),
            ("FT-SHIP-400", True),
            ("FT-EXEMPT-910", False),
        ]
        assert [item.get("custom_field_tax_code") for item in payload] == ["FT-SOFT-200", "FT-SHIP-400", "FT-EXEMPT-910"]
        assert not result.already_applied

    def test_nothing_to_write_once_codes_and_tax_are_on_the_lines(self) -> None:
        invoice = _invoice(
            address="Austin, TX 78701",
            lines=[{"id": 1, "amount": 100, "tax": "6.25", "custom_field_tax_code": "FT-GOODS-100"}],
        )

        result = compute_invoice_tax(invoice=invoice, default_rate="", workspace=_workspace(nexus=["TX"]))

        assert result.already_applied
