from decimal import Decimal

from tax import (
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
        invoice = _invoice(address="Portland, OR 97201", lines=[{"id": 1, "amount": 100, "tax": 0}])

        result = compute_invoice_tax(invoice=invoice, default_rate="5.00")

        assert result.rate == Decimal("5.00")
        assert result.description == "FakeTax OR 5.00%"

    def test_needs_rate_without_table_match_or_default(self) -> None:
        invoice = _invoice(address="Portland, OR 97201", lines=[{"id": 1, "amount": 100, "tax": 0}])

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
