"""Set customer-visible transaction timestamps."""
from __future__ import annotations

from django.utils import timezone


def apply_transaction_booking(tx, booked_at=None, *, settled: bool = True) -> None:
    ts = booked_at or timezone.now()
    tx.created_at = ts
    if settled:
        tx.completed_at = ts
    else:
        tx.completed_at = None
    tx.save(update_fields=['created_at', 'completed_at'])


def apply_transaction_booking_with_fees(tx, booked_at=None, *, settled: bool = True) -> None:
    """Apply booking date to a parent transaction and any linked FEE rows."""
    from .models import Transaction

    ts = booked_at or timezone.now()
    apply_transaction_booking(tx, ts, settled=settled)
    fee_settled = settled and tx.status == Transaction.Status.COMPLETED
    parent_id = str(tx.id)
    for fee_tx in Transaction.objects.filter(
        transaction_type=Transaction.TransactionType.FEE,
        metadata__parent_transaction_id=parent_id,
    ):
        apply_transaction_booking(fee_tx, ts, settled=fee_settled)
