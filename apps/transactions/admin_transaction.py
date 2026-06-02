"""Admin overrides for transactions (edit / delete with balance correction)."""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from django.db import transaction as db_transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import Account
from .models import Transaction, TransactionFee


class AdminTransactionError(Exception):
    pass


def _credited_transfer_amount(tx: Transaction) -> Decimal:
    meta = tx.metadata or {}
    if meta.get('pending_credited_amount') is not None:
        return Decimal(str(meta['pending_credited_amount']))
    rate = Decimal(str(tx.exchange_rate or 1))
    return tx.amount * rate


def compute_settled_balance_deltas(tx: Transaction) -> dict[UUID, Decimal]:
    """Balance impact of a COMPLETED transaction (excludes ledger-only FEE lines)."""
    if tx.status != Transaction.Status.COMPLETED:
        return {}

    t = tx.transaction_type
    deltas: dict[UUID, Decimal] = {}

    if t == Transaction.TransactionType.FEE:
        return {}

    if t == Transaction.TransactionType.DEPOSIT and tx.to_account_id:
        deltas[tx.to_account_id] = tx.amount - tx.fee_amount

    elif t == Transaction.TransactionType.WITHDRAWAL and tx.from_account_id:
        deltas[tx.from_account_id] = -(tx.amount + tx.fee_amount)

    elif t in (
        Transaction.TransactionType.TRANSFER_INTERNAL,
        Transaction.TransactionType.TRANSFER_EXTERNAL,
        Transaction.TransactionType.TRANSFER_INTERNATIONAL,
    ):
        credited = _credited_transfer_amount(tx)
        if tx.from_account_id:
            deltas[tx.from_account_id] = -(tx.amount + tx.fee_amount)
        if tx.to_account_id:
            deltas[tx.to_account_id] = credited

    elif t == Transaction.TransactionType.REVERSAL:
        if tx.to_account_id:
            deltas[tx.to_account_id] = tx.amount
        if tx.from_account_id:
            deltas[tx.from_account_id] = -tx.amount

    elif t == Transaction.TransactionType.LOAN_DISBURSEMENT and tx.to_account_id:
        deltas[tx.to_account_id] = tx.amount

    elif t == Transaction.TransactionType.LOAN_PAYMENT and tx.from_account_id:
        deltas[tx.from_account_id] = -(tx.amount + tx.fee_amount)

    elif t == Transaction.TransactionType.INTEREST and tx.to_account_id:
        deltas[tx.to_account_id] = tx.amount

    return deltas


def _apply_deltas(deltas: dict[UUID, Decimal], *, reverse: bool = False) -> None:
    sign = Decimal('-1') if reverse else Decimal('1')
    for account_id, delta in deltas.items():
        if delta == 0:
            continue
        acc = Account.objects.select_for_update().get(id=account_id)
        change = delta * sign
        acc.balance += change
        acc.available_balance += change
        acc.save(update_fields=['balance', 'available_balance', 'updated_at'])


def _child_fee_transactions(tx: Transaction):
    """Service-fee rows linked to a parent (not failed-deposit mirror lines)."""
    ref = tx.reference_number
    parent_id = str(tx.id)
    return Transaction.objects.filter(
        transaction_type=Transaction.TransactionType.FEE,
    ).filter(
        Q(metadata__parent_transaction_id=parent_id) | Q(description__icontains=ref),
    ).exclude(metadata__has_key='mirror_kind')


def _fee_type_for_transaction_type(transaction_type: str) -> str | None:
    t = transaction_type
    if t == Transaction.TransactionType.DEPOSIT:
        return TransactionFee.FeeType.DEPOSIT
    if t == Transaction.TransactionType.WITHDRAWAL:
        return TransactionFee.FeeType.WITHDRAWAL
    if t == Transaction.TransactionType.TRANSFER_INTERNATIONAL:
        return TransactionFee.FeeType.TRANSFER_INTERNATIONAL
    if t in (
        Transaction.TransactionType.TRANSFER_INTERNAL,
        Transaction.TransactionType.TRANSFER_EXTERNAL,
    ):
        return TransactionFee.FeeType.TRANSFER_LOCAL
    return None


def _recalculate_fee_amount(tx: Transaction, principal: Decimal, *, transaction_type: str | None = None) -> Decimal:
    fee_type = _fee_type_for_transaction_type(transaction_type or tx.transaction_type)
    if not fee_type:
        return Decimal(str(tx.fee_amount or 0))
    from .services import _get_fee

    return _get_fee(fee_type, principal)


def _sync_child_fee_transactions(tx: Transaction) -> None:
    """Align linked FEE rows with parent amount, status, currency, dates, and description."""
    from .booking import apply_transaction_booking
    from .narration import build_fee_system_narration, finalize_transaction_description
    from .services import _record_fee

    fee_amount = Decimal(str(tx.fee_amount or 0))
    children = list(_child_fee_transactions(tx).select_for_update())
    booked_at = tx.completed_at or tx.created_at
    settled = tx.status == Transaction.Status.COMPLETED

    if fee_amount <= 0:
        for fee_tx in children:
            fee_tx.delete()
        return

    if not children:
        account = tx.from_account or tx.to_account
        if account and settled:
            _record_fee(account, fee_amount, tx, tx.initiated_by)
        return

    fee_system = build_fee_system_narration(tx.reference_number)
    for fee_tx in children:
        fee_tx.amount = fee_amount
        fee_tx.currency = tx.currency
        fee_tx.status = tx.status
        meta = dict(fee_tx.metadata or {})
        meta['parent_transaction_id'] = str(tx.id)
        meta['fee_for_reference'] = tx.reference_number
        fee_tx.metadata = meta
        if booked_at is not None:
            apply_transaction_booking(fee_tx, booked_at, settled=settled)
        elif settled and not fee_tx.completed_at:
            fee_tx.completed_at = timezone.now()
        elif not settled:
            fee_tx.completed_at = None
        finalize_transaction_description(fee_tx, fee_system)
        fee_tx.save(
            update_fields=[
                'amount',
                'currency',
                'status',
                'description',
                'metadata',
                'created_at',
                'completed_at',
            ],
        )


@db_transaction.atomic
def admin_update_transaction(transaction_id: str, *, updates: dict, actor) -> Transaction:
    tx = Transaction.objects.select_for_update().get(id=transaction_id)
    old_status = tx.status
    old_deltas = compute_settled_balance_deltas(tx)

    allowed = {
        'amount', 'status', 'description', 'transaction_type', 'fee_amount', 'currency', 'transaction_at',
    }
    patch = {k: v for k, v in updates.items() if k in allowed and v is not None}
    booked_at = patch.pop('transaction_at', None)
    if not patch and booked_at is None:
        raise AdminTransactionError('No valid fields to update.')

    if 'amount' in patch:
        amount = Decimal(str(patch['amount']))
        if amount <= 0:
            raise AdminTransactionError('Amount must be positive.')
        patch['amount'] = amount

    if 'fee_amount' in patch:
        fee = Decimal(str(patch['fee_amount']))
        if fee < 0:
            raise AdminTransactionError('Fee cannot be negative.')
        patch['fee_amount'] = fee

    if 'status' in patch:
        status = str(patch['status']).upper()
        valid = {c[0] for c in Transaction.Status.choices}
        if status not in valid:
            raise AdminTransactionError('Invalid status.')
        patch['status'] = status

    if 'amount' in patch and 'fee_amount' not in patch:
        patch['fee_amount'] = _recalculate_fee_amount(
            tx,
            patch['amount'],
            transaction_type=patch.get('transaction_type'),
        )

    if old_status == Transaction.Status.COMPLETED:
        _apply_deltas(old_deltas, reverse=True)

    for field, value in patch.items():
        setattr(tx, field, value)

    if booked_at is not None:
        from .booking import apply_transaction_booking

        apply_transaction_booking(
            tx,
            booked_at,
            settled=tx.status == Transaction.Status.COMPLETED,
        )
    elif tx.status == Transaction.Status.COMPLETED and not tx.completed_at:
        tx.completed_at = timezone.now()
    elif tx.status != Transaction.Status.COMPLETED:
        tx.completed_at = None

    tx.save()

    new_deltas = compute_settled_balance_deltas(tx)
    if tx.status == Transaction.Status.COMPLETED:
        _apply_deltas(new_deltas)

    meta = dict(tx.metadata or {})
    meta['admin_last_edit'] = {
        'by': str(getattr(actor, 'id', '')),
        'at': timezone.now().isoformat(),
        'fields': list(patch.keys()) + (['transaction_at'] if booked_at is not None else []),
    }
    tx.metadata = meta
    tx.save(update_fields=['metadata'])

    _sync_child_fee_transactions(tx)

    return tx


def _admin_delete_one_transaction(tx: Transaction) -> int:
    """Delete one transaction row, its fee children, and any reversal children (balance-corrected)."""
    deleted = 0
    for rev in list(tx.reversals.select_for_update().order_by('created_at')):
        deleted += _admin_delete_one_transaction(rev)

    deltas = compute_settled_balance_deltas(tx)
    if deltas:
        _apply_deltas(deltas, reverse=True)

    for fee_tx in _child_fee_transactions(tx):
        fee_tx.delete()
        deleted += 1

    tx.delete()
    return deleted + 1


@db_transaction.atomic
def admin_delete_transactions(transaction_ids: list[str], *, actor) -> int:
    if not transaction_ids:
        return 0

    id_set = {str(i) for i in transaction_ids}
    txs = list(
        Transaction.objects.select_for_update()
        .filter(id__in=id_set)
        .order_by('created_at'),
    )
    if not txs:
        return 0

    deleted = 0
    for tx in txs:
        if not Transaction.objects.filter(pk=tx.pk).exists():
            continue
        deleted += _admin_delete_one_transaction(tx)

    return deleted


def build_admin_deposit_audit_note(
    account: Account,
    deposit_method: str,
    status: str,
    amount: Decimal,
    deposit_source: dict,
    actor,
) -> str:
    from .deposit_source import METHOD_LABEL, build_deposit_narration

    actor_label = (getattr(actor, 'full_name', None) or '').strip() or getattr(actor, 'email', 'Admin')
    method = METHOD_LABEL.get(deposit_method, deposit_method)
    narration = build_deposit_narration(deposit_method, deposit_source or {})
    tail = account.account_number[-4:] if account.account_number else '????'
    ts = timezone.now().strftime('%Y-%m-%d %H:%M UTC')
    return (
        f'Admin deposit · {actor_label} · {method} · {status} · '
        f'{amount} {account.currency.code} · acct ····{tail} · {narration} · {ts}'
    )
