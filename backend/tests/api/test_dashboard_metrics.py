from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from src.models.invoice import Invoice, InvoiceItem
from src.models.payment import Payment
from src.models.product import Product


def _create_product(client, sku, name, price, **extra):
    payload = {"sku": sku, "name": name, "price": price, "gst_rate": 18}
    payload.update(extra)
    response = client.post("/api/products/", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _create_ledger(client, name):
    response = client.post(
        "/api/ledgers/",
        json={"name": name, "address": "1 Market St", "phone_number": "1234567890"},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_dashboard_metrics_aggregates_across_all_invoices(client):
    # Two products, one with a reorder level so we can exercise low-stock logic.
    low_product = _create_product(client, "DASH-LOW", "Low Stock Widget", 100, reorder_level=10)
    ok_product = _create_product(client, "DASH-OK", "Healthy Widget", 50, reorder_level=0)

    # Drive stock below the reorder level on the first product.
    assert client.post("/api/inventory/adjust", json={"product_id": low_product, "quantity": 5}).status_code == 200
    assert client.post("/api/inventory/adjust", json={"product_id": ok_product, "quantity": 80}).status_code == 200

    # Create a sales invoice with two line items.
    ledger_id = _create_ledger(client, "Acme Buyer")
    invoice_payload = {
        "voucher_type": "sales",
        "ledger_id": ledger_id,
        "items": [
            {"product_id": low_product, "quantity": 2, "unit_price": 100, "gst_rate": 18},
            {"product_id": ok_product, "quantity": 4, "unit_price": 50, "gst_rate": 18},
        ],
    }
    inv_res = client.post("/api/invoices/", json=invoice_payload)
    assert inv_res.status_code == 200, inv_res.text

    res = client.get("/api/dashboard/metrics")
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["catalog"]["total_products"] == 2
    assert data["inventory"]["tracked_products"] == 2
    assert data["inventory"]["low_stock_count"] == 1  # only the reorder-level product
    assert data["sales"]["sales_invoice_count"] == 1
    assert data["sales"]["total_sales"] > 0
    assert data["receivables"]["unpaid_count"] == 1
    assert data["receivables"]["outstanding_amount"] > 0

    # Charts always span the trailing 12 months and surface the sold products.
    assert len(data["charts"]["monthly"]) == 12
    assert any(point["sales"] > 0 for point in data["charts"]["monthly"])
    top_names = {p["name"] for p in data["charts"]["top_products"]}
    assert "Low Stock Widget" in top_names


def test_dashboard_metrics_empty_company(client):
    res = client.get("/api/dashboard/metrics")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["catalog"]["total_products"] == 0
    assert data["sales"]["total_sales"] == 0
    assert data["receivables"]["outstanding_amount"] == 0
    assert len(data["charts"]["monthly"]) == 12
    assert data["charts"]["top_products"] == []


def _record_product_sale(db_session, lines, *, discount=0, voucher_type="sales", status="active"):
    """Persist line and invoice totals as the invoice processor stores them."""
    taxable = sum(taxable for _, taxable, _ in lines)
    tax = sum(tax for _, _, tax in lines)
    invoice = Invoice(
        company_id=db_session.get(Product, lines[0][0]).company_id,
        created_by=1,
        voucher_type=voucher_type,
        status=status,
        taxable_amount=taxable,
        total_tax_amount=tax,
        total_amount=taxable + tax - discount,
    )
    invoice.items = [
        InvoiceItem(product_id=product_id, quantity=1, unit_price=Decimal(taxable),
                    taxable_amount=taxable, tax_amount=tax, line_total=taxable + tax)
        for product_id, taxable, tax in lines
    ]
    db_session.add(invoice)
    db_session.commit()


def _top_products(client):
    response = client.get("/api/dashboard/metrics")
    assert response.status_code == 200, response.text
    return response.json()["charts"]["top_products"]


def test_dashboard_top_product_revenue_subtracts_invoice_discount(client):
    product = _create_product(client, "DASH-DISC", "Discounted Widget", 1100, initial_quantity=1)
    ledger = _create_ledger(client, "Discount buyer")
    response = client.post("/api/invoices/", json={
        "voucher_type": "sales",
        "ledger_id": ledger,
        "discount_type": "net",
        "discount_value": 100,
        "items": [{
            "product_id": product, "quantity": 1, "unit_price": 1100, "gst_rate": 0,
            "discount_type": "net", "discount_value": 100,
        }],
    })
    assert response.status_code == 200, response.text
    assert response.json()["total_amount"] == 900

    assert [(row["quantity"], row["revenue"]) for row in _top_products(client)] == [(1, 900)]


def test_dashboard_top_products_split_discount_by_gst_inclusive_line_total(client, db_session):
    exempt = _create_product(client, "DASH-EX", "Exempt Widget", 1000)
    taxed = _create_product(client, "DASH-TAX", "Taxed Widget", 1000)
    _record_product_sale(db_session, [(exempt, 1000, 0), (taxed, 1000, 280)], discount=228)

    assert [(row["product_id"], row["revenue"]) for row in _top_products(client)] == [
        (taxed, 1152), (exempt, 900),
    ]


def test_dashboard_top_products_ranks_net_revenue_before_taking_five(client, db_session):
    discounted = _create_product(client, "DASH-DROP", "Discounted", 1000)
    _record_product_sale(db_session, [(discounted, 1000, 0)], discount=600)
    _record_product_sale(db_session, [(discounted, 2000, 0)], status="cancelled")
    _record_product_sale(db_session, [(discounted, 2000, 0)], voucher_type="purchase")

    expected = []
    for revenue in [900, 800, 700, 600, 500]:
        product = _create_product(client, f"DASH-RANK-{revenue}", f"Widget {revenue}", revenue)
        _record_product_sale(db_session, [(product, revenue, 0)])
        expected.append((product, revenue))

    assert [(row["product_id"], row["revenue"]) for row in _top_products(client)] == expected


@pytest.mark.parametrize("today,next_month_start", [
    (date(2026, 11, 12), datetime(2026, 12, 1)),
    (date(2026, 12, 12), datetime(2027, 1, 1)),
    (date(2028, 2, 12), datetime(2028, 3, 1)),
])
def test_dashboard_current_month_stops_at_next_month(
    client, db_session, monkeypatch, today, next_month_start
):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return today

    monkeypatch.setattr("src.api.routes.dashboard.date", FixedDate)
    product = _create_product(client, "DASH-MONTH", "Monthly Widget", 100)
    company_id = db_session.get(Product, product).company_id
    month_start = datetime(today.year, today.month, 1)

    for timestamp, amount in [
        (month_start - timedelta(microseconds=1), 10),
        (month_start, 20),
        (next_month_start - timedelta(microseconds=1), 30),
        (next_month_start, 40),
        (next_month_start + timedelta(days=3), 50),
    ]:
        db_session.add(Invoice(
            company_id=company_id, created_by=1, voucher_type="sales",
            invoice_date=timestamp, total_amount=amount,
        ))
        db_session.add(Payment(
            company_id=company_id, created_by=1, voucher_type="receipt",
            date=timestamp, amount=amount / 2,
        ))
    db_session.commit()

    response = client.get("/api/dashboard/metrics")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["sales"]["this_month_sales"] == 50
    assert data["payments"]["this_month_received"] == 25
    assert data["charts"]["monthly"][-1]["sales"] == 50
    assert data["charts"]["monthly"][-1]["receipts"] == 25
    assert data["sales"]["total_sales"] == 150
    assert data["payments"]["received_total"] == 75


def _overdue_bucket_totals(ageing_body):
    overdue_buckets = [b for b in ageing_body["buckets"] if b["key"] != "not_yet_due"]
    return (
        sum(b["total_remaining"] for b in overdue_buckets),
        sum(b["invoice_count"] for b in overdue_buckets),
    )


def test_dashboard_receivables_excludes_ledger_offset_by_larger_payable(client):
    today = date.today()
    overdue_due_date = (today - timedelta(days=40)).isoformat()
    product = _create_product(client, "DASH-OFFSET", "Offset Widget", 200, initial_quantity=10)
    alpha = _create_ledger(client, "Alpha")

    sales_res = client.post(
        "/api/invoices/",
        json={
            "voucher_type": "sales",
            "ledger_id": alpha,
            "invoice_date": today.isoformat(),
            "due_date": overdue_due_date,
            "items": [{"product_id": product, "quantity": 1, "unit_price": 200, "gst_rate": 18}],
        },
    )
    assert sales_res.status_code == 200, sales_res.text
    assert sales_res.json()["total_amount"] == 236

    purchase_res = client.post(
        "/api/invoices/",
        json={
            "voucher_type": "purchase",
            "ledger_id": alpha,
            "supplier_invoice_number": "ALPHA-PUR-1",
            "invoice_date": today.isoformat(),
            "items": [{"product_id": product, "quantity": 1, "unit_price": 300, "gst_rate": 18}],
        },
    )
    assert purchase_res.status_code == 200, purchase_res.text
    assert purchase_res.json()["total_amount"] == 354

    metrics_res = client.get("/api/dashboard/metrics")
    assert metrics_res.status_code == 200, metrics_res.text
    receivables = metrics_res.json()["receivables"]

    ageing_res = client.get("/api/invoices/dues/ageing")
    assert ageing_res.status_code == 200, ageing_res.text
    ageing = ageing_res.json()
    ageing_overdue_amount, ageing_overdue_count = _overdue_bucket_totals(ageing)

    assert ageing["total_remaining"] == 0
    assert ageing_overdue_amount == 0
    assert receivables["outstanding_amount"] == ageing["total_remaining"]
    assert receivables["overdue_amount"] == ageing_overdue_amount
    assert receivables["overdue_count"] == ageing_overdue_count
    assert receivables["unpaid_count"] == 1

    # Once the offsetting purchase invoice is cancelled, both surfaces report the overdue sale again.
    cancel_res = client.delete(f"/api/invoices/{purchase_res.json()['id']}")
    assert cancel_res.status_code == 200, cancel_res.text

    receivables_after = client.get("/api/dashboard/metrics").json()["receivables"]
    ageing_after = client.get("/api/invoices/dues/ageing").json()
    after_overdue_amount, after_overdue_count = _overdue_bucket_totals(ageing_after)

    assert ageing_after["total_remaining"] == 236
    assert receivables_after["outstanding_amount"] == ageing_after["total_remaining"]
    assert receivables_after["overdue_amount"] == after_overdue_amount == 236
    assert receivables_after["overdue_count"] == after_overdue_count == 1


def test_dashboard_receivables_matches_ageing_across_offset_and_open_ledgers(client):
    today = date.today()
    overdue_due_date = (today - timedelta(days=20)).isoformat()
    future_due_date = (today + timedelta(days=10)).isoformat()
    product = _create_product(client, "DASH-MIX", "Mixed Widget", 100, gst_rate=0, initial_quantity=20)

    net_zero_ledger = _create_ledger(client, "Net Zero Party")
    partial_offset_ledger = _create_ledger(client, "Partial Offset Party")
    upcoming_ledger = _create_ledger(client, "Upcoming Party")

    # 1. Exact net zero (200 receivable vs 200 payable) -> hidden from Dues & Dashboard totals.
    assert client.post(
        "/api/invoices/",
        json={
            "voucher_type": "sales",
            "ledger_id": net_zero_ledger,
            "invoice_date": today.isoformat(),
            "due_date": overdue_due_date,
            "items": [{"product_id": product, "quantity": 1, "unit_price": 200, "gst_rate": 0}],
        },
    ).status_code == 200
    assert client.post(
        "/api/invoices/",
        json={
            "voucher_type": "purchase",
            "ledger_id": net_zero_ledger,
            "supplier_invoice_number": "NZ-PUR-1",
            "invoice_date": today.isoformat(),
            "items": [{"product_id": product, "quantity": 1, "unit_price": 200, "gst_rate": 0}],
        },
    ).status_code == 200

    # 2. Sales exceed purchases (300 overdue receivable vs 100 payable) -> remains due (300).
    assert client.post(
        "/api/invoices/",
        json={
            "voucher_type": "sales",
            "ledger_id": partial_offset_ledger,
            "invoice_date": today.isoformat(),
            "due_date": overdue_due_date,
            "items": [{"product_id": product, "quantity": 1, "unit_price": 300, "gst_rate": 0}],
        },
    ).status_code == 200
    assert client.post(
        "/api/invoices/",
        json={
            "voucher_type": "purchase",
            "ledger_id": partial_offset_ledger,
            "supplier_invoice_number": "PO-PUR-1",
            "invoice_date": today.isoformat(),
            "items": [{"product_id": product, "quantity": 1, "unit_price": 100, "gst_rate": 0}],
        },
    ).status_code == 200

    # 3. Not yet due customer (150 receivable, no payable) -> in outstanding, not overdue.
    assert client.post(
        "/api/invoices/",
        json={
            "voucher_type": "sales",
            "ledger_id": upcoming_ledger,
            "invoice_date": today.isoformat(),
            "due_date": future_due_date,
            "items": [{"product_id": product, "quantity": 1, "unit_price": 150, "gst_rate": 0}],
        },
    ).status_code == 200

    receivables = client.get("/api/dashboard/metrics").json()["receivables"]
    ageing = client.get("/api/invoices/dues/ageing").json()
    ageing_overdue_amount, ageing_overdue_count = _overdue_bucket_totals(ageing)

    assert ageing["total_remaining"] == 450
    assert ageing_overdue_amount == 300
    assert ageing_overdue_count == 1
    assert receivables["outstanding_amount"] == ageing["total_remaining"]
    assert receivables["overdue_amount"] == ageing_overdue_amount
    assert receivables["overdue_count"] == ageing_overdue_count
    assert receivables["unpaid_count"] == 3
