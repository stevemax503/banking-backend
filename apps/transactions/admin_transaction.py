"""Admin overrides for transactions (edit / delete with balance correction)."""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from django.db import transaction as db_transaction
from django.utils import timezone

from apps.accounts.models import Account
from .models import Transaction


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
    ref = tx.reference_number
    return Transaction.objects.filter(
        transaction_type=Transaction.TransactionType.FEE,
        description__icontains=ref,
    )


@db_transaction.atomic
def admin_update_transaction(transaction_id: str, *, updates: dict, actor) -> Transaction:
    tx = Transaction.objects.select_for_update().get(id=transaction_id)
    old_status = tx.status
    old_deltas = compute_settled_balance_deltas(tx)

    allowed = {'amount', 'status', 'description', 'transaction_type', 'fee_amount', 'currency'}
    patch = {k: v for k, v in updates.items() if k in allowed and v is not None}
    if not patch:
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

    if old_status == Transaction.Status.COMPLETED:
        _apply_deltas(old_deltas, reverse=True)

    for field, value in patch.items():
        setattr(tx, field, value)

    if tx.status == Transaction.Status.COMPLETED and not tx.completed_at:
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
        'fields': list(patch.keys()),
    }
    tx.metadata = meta
    tx.save(update_fields=['metadata'])

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
