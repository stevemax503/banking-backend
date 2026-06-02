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
