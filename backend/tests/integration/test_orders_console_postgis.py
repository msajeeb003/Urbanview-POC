"""Orders as the admin console works them: the experts a manager assigns to (managers only), the
order's timeline from its audit entries (payment with amount and bank reference, refund details),
replacing a delivered report (a note is required, the file is versioned and the delivery e-mail
goes out again), an expert assigned only once the order is paid, a refund after delivery, and the
queue's columns (parcel, planned parcel, data version seen, turnaround, delivered)."""

from __future__ import annotations

import pytest

from core.seeds import placeholder_pdf
from tests.helpers import make_client
from tests.integration import test_orders_postgis as orders

pytestmark = pytest.mark.integration

# The order tests' app (eager e-mail with a transport double), clean-up and helpers.
order_app = orders.order_app
mailer = orders.mailer
_eager_mail = orders._eager_mail
_clean_orders = orders._clean_orders
FORM, PDF, auth = orders.FORM, orders.PDF, orders.auth
staff_token, order_id_of = orders.staff_token, orders.order_id_of

PDF_V2 = placeholder_pdf("Expert analysis, corrected", 2)


async def test_experts_timeline_refund_and_a_replaced_report(order_app, mailer):
    app = order_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        reference = (await client.post("/v1/orders", json=FORM)).json()["reference"]
        other_ref = (
            await client.post("/v1/orders", json={**FORM, "email": "d@example.com"})
        ).json()["reference"]
        oid = await order_id_of(app, reference)
        other = await order_id_of(app, other_ref)
        expert_id, expert = await staff_token(app, "expert@example.com", "expert")

        experts = await client.get("/v1/admin/orders/experts", headers=auth())
        experts_as_expert = await client.get("/v1/admin/orders/experts", headers=auth(expert))
        await client.post(
            f"/v1/admin/orders/{oid}/payment",
            json={
                "status": "received",
                "amount_eur": 200,
                "received_on": "2026-09-25",
                "reference": "BANK-7",
            },
            headers=auth(),
        )
        await client.post(
            f"/v1/admin/orders/{oid}/assign", json={"expert_user_id": expert_id}, headers=auth()
        )
        first = await client.post(
            f"/v1/admin/orders/{oid}/report",
            files={"file": ("report.pdf", PDF, "application/pdf")},
            headers=auth(expert),
        )
        sent_before = len(mailer.sent)
        no_note = await client.post(
            f"/v1/admin/orders/{oid}/report",
            files={"file": ("report-v2.pdf", PDF_V2, "application/pdf")},
            headers=auth(expert),
        )
        replaced = await client.post(
            f"/v1/admin/orders/{oid}/report",
            files={"file": ("report-v2.pdf", PDF_V2, "application/pdf")},
            data={"note": "Corrected the parking figure on page 2"},
            headers=auth(expert),
        )
        await client.post(
            f"/v1/admin/orders/{other}/payment",
            json={"status": "received", "amount_eur": 200, "reference": "BANK-8"},
            headers=auth(),
        )
        refunded = await client.post(
            f"/v1/admin/orders/{other}/payment",
            json={
                "status": "refunded",
                "amount_eur": 200,
                "received_on": "2026-09-26",
                "reference": "REFUND-8",
                "note": "customer cancelled",
            },
            headers=auth(),
        )

    assert experts.status_code == 200
    assert [e["email"] for e in experts.json()] == ["expert@example.com"]
    assert experts_as_expert.status_code == 403

    assert first.status_code == 200 and first.json()["status"] == "delivered"
    assert first.json()["report_versions"] == 1
    assert no_note.status_code == 422  # replacing a delivered report says why
    assert replaced.status_code == 200, replaced.text
    body = replaced.json()
    assert body["status"] == "delivered" and body["report_versions"] == 2
    assert body["report"]["original_filename"] == "report-v2.pdf"
    assert len(mailer.sent) == sent_before + 1  # the new link went out
    actions = [e["action"] for e in body["timeline"]]
    assert actions.count("order.report") == 2
    payment = next(e for e in body["timeline"] if e["action"] == "order.payment")
    assert (
        payment["details"]["amount_eur"] == 200 and payment["details"]["bank_reference"] == "BANK-7"
    )
    assert payment["details"]["received_on"] == "2026-09-25"
    replacement = [e for e in body["timeline"] if e["action"] == "order.report"][-1]
    assert replacement["details"]["version"] == 2 and replacement["details"]["replaces_file_id"]
    assert replacement["note"] == "Corrected the parking figure on page 2"

    assert refunded.status_code == 200 and refunded.json()["status"] == "refunded"
    refund = [e for e in refunded.json()["timeline"] if e["after"] == {"status": "refunded"}][-1]
    assert refund["details"]["refund_amount_eur"] == 200
    assert refund["details"]["refunded_on"] == "2026-09-26"
    assert refund["details"]["bank_reference"] == "REFUND-8"
    assert refund["note"] == "customer cancelled"


async def test_assignment_waits_for_the_payment_and_a_delivered_order_can_be_refunded(order_app):
    app = order_app
    async with app.router.lifespan_context(app), make_client(app) as client:
        reference = (await client.post("/v1/orders", json=FORM)).json()["reference"]
        urban_form = {
            **FORM,
            "email": "u@example.com",
            "location": {"parcel_type": "urban", "parcel_id": 1},
        }
        urban_ref = (await client.post("/v1/orders", json=urban_form)).json()["reference"]
        oid = await order_id_of(app, reference)
        expert_id, expert = await staff_token(app, "expert@example.com", "expert")

        too_early = await client.post(
            f"/v1/admin/orders/{oid}/assign", json={"expert_user_id": expert_id}, headers=auth()
        )
        await client.post(
            f"/v1/admin/orders/{oid}/payment",
            json={"status": "received", "amount_eur": 200, "reference": "BANK-9"},
            headers=auth(),
        )
        assigned = await client.post(
            f"/v1/admin/orders/{oid}/assign", json={"expert_user_id": expert_id}, headers=auth()
        )
        delivered = await client.post(
            f"/v1/admin/orders/{oid}/report",
            files={"file": ("report.pdf", PDF, "application/pdf")},
            headers=auth(expert),
        )
        # more than the EUR 200 that came in: refused, the order stays delivered
        too_much = await client.post(
            f"/v1/admin/orders/{oid}/payment",
            json={
                "status": "refunded",
                "amount_eur": 250,
                "received_on": "2026-09-30",
                "reference": "REFUND-9",
            },
            headers=auth(),
        )
        still_delivered = await client.get(f"/v1/admin/orders/{oid}", headers=auth())
        refunded = await client.post(
            f"/v1/admin/orders/{oid}/payment",
            json={
                "status": "refunded",
                "amount_eur": 200,
                "received_on": "2026-09-30",
                "reference": "REFUND-9",
                "note": "goodwill refund after delivery",
            },
            headers=auth(),
        )
        refunded_again = await client.post(
            f"/v1/admin/orders/{oid}/payment", json={"status": "refunded"}, headers=auth()
        )
        queue = await client.get("/v1/admin/orders", headers=auth())

    # an expert works on a paid order: no assignment while the transfer is due
    assert too_early.status_code == 409
    assert too_early.json()["error"]["details"]["status"] == "pending_payment"
    assert assigned.status_code == 200 and assigned.json()["status"] == "in_progress"
    assert delivered.status_code == 200 and delivered.json()["status"] == "delivered"
    # a refund never exceeds what was received
    assert too_much.status_code == 422, too_much.text
    assert too_much.json()["error"]["details"] == [
        {
            "loc": ["body", "amount_eur"],
            "msg": "a refund cannot be more than the 200.00 EUR received",
        }
    ]
    assert still_delivered.json()["status"] == "delivered"
    # a delivered order can still be refunded (amount, date, reference on the audit row)
    assert refunded.status_code == 200, refunded.text
    body = refunded.json()
    assert body["status"] == "refunded" and body["refunded_at"] and body["delivered_at"]
    refund = [e for e in body["timeline"] if e["after"] == {"status": "refunded"}][-1]
    assert refund["before"] == {"status": "delivered"}
    assert refund["details"]["refund_amount_eur"] == 200
    assert refund["details"]["bank_reference"] == "REFUND-9"
    assert refunded_again.status_code == 409  # refunded is final

    # the queue, newest first: KO + number, the planned parcel, the data version seen, turnaround
    items = queue.json()["items"]
    assert [o["reference"] for o in items] == [urban_ref, reference]
    urban, cadastral = items
    for order in (urban, cadastral):
        assert order["ko_and_number"] == "KO Podgorica I, 1042"
        assert order["planned_parcel"] == "UP 12"
        assert order["data_version"] == "sample-2026-09-22" and order["data_version_no"] == 1
        assert order["turnaround_business_days"] == 5 and order["price_eur"] > 0
    assert cadastral["delivered_at"] and urban["delivered_at"] is None
