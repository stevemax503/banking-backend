"""
Double-entry transaction processing service.
All balance mutations go through this module.
"""
import re
import uuid
from decimal import Decimal
from django.db import transaction as db_transaction
from django.utils import timezone

from apps.accounts.models import Account
from apps.accounts.uae_iban import (
    DOMESTIC_ACCOUNT_NUMBER_MAX_LENGTH,
    DOMESTIC_ACCOUNT_NUMBER_MIN_LENGTH,
    UaeAccountIdentifierError,
    compact_identifier,
    extract_domestic_from_uae_iban,
    is_valid_domestic_account_number,
    is_valid_uae_iban,
    normalize_to_domestic_account_number,
)
from .models import Transaction, TransactionFee, ExchangeRate
from .regulated_flow import applicable_compliance_lines, international_requires_regulated_session
from .regulated_models import RegulatedTransferSession


class InsufficientFundsError(Exception):
    pass


class AccountStatusError(Exception):
    pass


class TransactionError(Exception):
    pass


DESTINATION_ACCOUNT_NUMBER_MIN_LENGTH = DOMESTIC_ACCOUNT_NUMBER_MIN_LENGTH
DESTINATION_ACCOUNT_NUMBER_MAX_LENGTH = DOMESTIC_ACCOUNT_NUMBER_MAX_LENGTH


def normalize_destination_account_number(raw: str) -> str:
    """Accept UAE IBAN or 11–16 digit domestic account number."""
    try:
        return normalize_to_domestic_account_number(raw)
    except UaeAccountIdentifierError as exc:
        raise TransactionError(str(exc)) from exc


def _get_fee(tx_type: str, amount: Decimal) -> Decimal:
    fee_obj = TransactionFee.objects.filter(fee_type=tx_type, is_active=True).first()
    if not fee_obj:
        return Decimal('0')
    return Decimal(str(fee_obj.calculate(amount)))


def _get_fee_row(fee_type: str) -> TransactionFee | None:
    return TransactionFee.objects.filter(fee_type=fee_type, is_active=True).first()


def _transfer_fee_type(tx_type: str) -> str:
    if tx_type == Transaction.TransactionType.TRANSFER_INTERNATIONAL:
        return TransactionFee.FeeType.TRANSFER_INTERNATIONAL
    return TransactionFee.FeeType.TRANSFER_LOCAL


def _transfer_requires_auth_otp(tx_type: str, fee_row: TransactionFee | None) -> bool:
    """Email OTP before transfer (independent of compliance-fee exemption)."""
    if tx_type in (
        Transaction.TransactionType.TRANSFER_INTERNAL,
        Transaction.TransactionType.TRANSFER_EXTERNAL,
        Transaction.TransactionType.TRANSFER_INTERNATIONAL,
    ):
        return True
    return bool(fee_row and fee_row.requires_otp)


def _transfer_fee_label(tx_type: str) -> str:
    if tx_type == Transaction.TransactionType.TRANSFER_EXTERNAL:
        return 'External transfer fee'
    if tx_type == Transaction.TransactionType.TRANSFER_INTERNATIONAL:
        return 'International transfer fee'
    return 'Transfer fee'


def _transfer_fee_line_dict(fee_type: str, fee: Decimal, fee_row: TransactionFee | None, tx_type: str) -> dict | None:
    """Omit transfer fee line when admin has no active fee row or calculated fee is zero."""
    if not fee_row or fee <= 0:
        return None
    return {
        'code': str(fee_type),
        'label': _transfer_fee_label(tx_type),
        'amount': str(fee),
        'line_kind': 'transfer',
    }


def _resolve_by_domestic_account_number(digits: str):
    """Exact match, then unique suffix match when user enters 11–15 digits of a longer stored number."""
    if not is_valid_domestic_account_number(digits):
        return None
    qs = Account.objects.filter(account_number=digits).select_related('currency', 'owner')
    acc = qs.first()
    if acc:
        return acc
    if len(digits) < DOMESTIC_ACCOUNT_NUMBER_MAX_LENGTH:
        suffix_qs = Account.objects.filter(account_number__endswith=digits).select_related('currency', 'owner')
        if suffix_qs.count() == 1:
            return suffix_qs.first()
    return None


def resolve_account_by_identifier(raw: str):
    """
    Resolve an account by UUID, UAE IBAN, or 11–16 digit account_number.
    Returns Account or None.
    """
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip()
    try:
        uid = uuid.UUID(s)
        return Account.objects.filter(id=uid).select_related('currency', 'owner').first()
    except (ValueError, TypeError, AttributeError):
        pass
    compact = compact_identifier(s)
    if compact.startswith('AE') and is_valid_uae_iban(compact):
        acc = Account.objects.filter(iban__iexact=compact).select_related('currency', 'owner').first()
        if acc:
            return acc
        domestic = extract_domestic_from_uae_iban(compact)
        return _resolve_by_domestic_account_number(domestic)
    try:
        domestic = normalize_destination_account_number(s)
    except TransactionError:
        domestic = None
    if domestic:
        acc = _resolve_by_domestic_account_number(domestic)
        if acc:
            return acc
    return Account.objects.filter(account_number__iexact=s).select_related('currency', 'owner').first()


def preview_transfer_fees(
    from_account_id: str,
    to_account_resolved_id: str,
    amount: Decimal,
    tx_type: str,
) -> dict:
    """Read-only fee / FX breakdown for UI (no balance mutation)."""
    amount = Decimal(str(amount))
    from_account = Account.objects.select_related('currency').get(id=from_account_id)
    to_account = Account.objects.select_related('currency').get(id=to_account_resolved_id)

    fee_type = _transfer_fee_type(tx_type)
    regulated_intl = (
        tx_type == Transaction.TransactionType.TRANSFER_INTERNATIONAL
        and international_requires_regulated_session(amount, from_account)
    )
    if regulated_intl:
        comp = applicable_compliance_lines(
            RegulatedTransferSession.Flow.INTERNATIONAL_TRANSFER, amount, from_account,
        )
        compliance_total = sum(Decimal(str(c.calculate(amount))) for c in comp)
        fee = _get_fee(fee_type, amount)
        fee_row = _get_fee_row(fee_type)
        exchange_rate = _get_exchange_rate_for_preview(from_account.currency.code, to_account.currency.code)
        same_ccy = from_account.currency.code == to_account.currency.code
        if fee_row and not fee_row.charge_upfront and same_ccy:
            total_debit = amount
            credited_amount = amount * exchange_rate - fee
        else:
            total_debit = amount + fee
            credited_amount = amount * exchange_rate
        if credited_amount < 0:
            raise TransactionError('Transfer amount must be greater than the fee for this pricing mode.')

        fees_lines = []
        transfer_line = _transfer_fee_line_dict(fee_type, fee, fee_row, tx_type)
        if transfer_line:
            fees_lines.append(transfer_line)
        for c in comp:
            fees_lines.append(
                {
                    'code': f'compliance:{c.code}',
                    'label': c.name,
                    'amount': str(c.calculate(amount)),
                    'line_kind': 'compliance',
                }
            )
        if from_account.currency.code != to_account.currency.code:
            fees_lines.append(
                {
                    'code': 'exchange',
                    'label': f'Exchange rate ({from_account.currency.code} → {to_account.currency.code})',
                    'amount': str(exchange_rate),
                    'is_rate': True,
                    'line_kind': 'exchange',
                },
            )
        return {
            'amount': str(amount),
            'currency': from_account.currency.code,
            'fee_total': str(fee),
            'base_transfer_fee': str(fee),
            'total_debit': str(total_debit),
            'compliance_fee_total': str(compliance_total),
            'fees': fees_lines,
            'exchange_rate': str(exchange_rate),
            'credited_amount': str(credited_amount),
            'credited_currency': to_account.currency.code,
            'destination': {
                'account_type': to_account.account_type,
                'last_four': to_account.account_number[-4:] if to_account.account_number else '',
            },
            'requires_otp': True,
            'requires_regulated_session': True,
            'charge_upfront': bool(fee_row.charge_upfront) if fee_row else True,
            'fee_billing': 'upfront' if (not fee_row or fee_row.charge_upfront or not same_ccy) else 'net_of_recipient',
        }

    fee = _get_fee(fee_type, amount)
    exchange_rate = _get_exchange_rate_for_preview(from_account.currency.code, to_account.currency.code)
    fee_row = _get_fee_row(fee_type)
    same_ccy = from_account.currency.code == to_account.currency.code
    if fee_row and not fee_row.charge_upfront and same_ccy:
        total_debit = amount
        credited_amount = amount * exchange_rate - fee
    else:
        total_debit = amount + fee
        credited_amount = amount * exchange_rate

    if credited_amount < 0:
        raise TransactionError('Transfer amount must be greater than the fee for this pricing mode.')

    fees_lines = []
    transfer_line = _transfer_fee_line_dict(fee_type, fee, fee_row, tx_type)
    if transfer_line:
        fees_lines.append(transfer_line)
    if from_account.currency.code != to_account.currency.code:
        fees_lines.append(
            {
                'code': 'exchange',
                'label': f'Exchange rate ({from_account.currency.code} → {to_account.currency.code})',
                'amount': str(exchange_rate),
                'is_rate': True,
            },
        )

    return {
        'amount': str(amount),
        'currency': from_account.currency.code,
        'fee_total': str(fee),
        'total_debit': str(total_debit),
        'fees': fees_lines,
        'exchange_rate': str(exchange_rate),
        'credited_amount': str(credited_amount),
        'credited_currency': to_account.currency.code,
        'destination': {
            'account_type': to_account.account_type,
            'last_four': to_account.account_number[-4:] if to_account.account_number else '',
        },
        'requires_otp': _transfer_requires_auth_otp(tx_type, fee_row),
        'requires_regulated_session': False,
        'charge_upfront': bool(fee_row.charge_upfront) if fee_row else True,
        'fee_billing': 'upfront' if (not fee_row or fee_row.charge_upfront or not same_ccy) else 'net_of_recipient',
    }


def preview_transfer_fees_for_account_number(
    from_account_id: str,
    to_account_number: str,
    amount: Decimal,
    tx_type: str,
) -> dict:
    """
    Fee preview for internal/external transfers using only a validated 16-digit account number.
    Assumes the recipient account is in the same currency as the sender (no FX lookup).
    """
    amount = Decimal(str(amount))
    to_account_number = normalize_destination_account_number(to_account_number)
    from_account = Account.objects.select_related('currency').get(id=from_account_id)

    if from_account.account_number == to_account_number:
        raise TransactionError('Source and destination accounts must be different.')

    fee_type = _transfer_fee_type(tx_type)
    fee = _get_fee(fee_type, amount)
    fee_row = _get_fee_row(fee_type)
    exchange_rate = Decimal('1')
    same_ccy = True
    if fee_row and not fee_row.charge_upfront and same_ccy:
        total_debit = amount
        credited_amount = amount - fee
    else:
        total_debit = amount + fee
        credited_amount = amount

    if credited_amount < 0:
        raise TransactionError('Transfer amount must be greater than the fee for this pricing mode.')

    fees_lines = []
    transfer_line = _transfer_fee_line_dict(fee_type, fee, fee_row, tx_type)
    if transfer_line:
        fees_lines.append(transfer_line)

    requires_otp = _transfer_requires_auth_otp(tx_type, fee_row)
    requires_regulated_session = False
    compliance_total = Decimal('0')
    base_transfer_fee = fee

    if tx_type == Transaction.TransactionType.TRANSFER_INTERNATIONAL and international_requires_regulated_session(
        amount, from_account,
    ):
        comp = applicable_compliance_lines(
            RegulatedTransferSession.Flow.INTERNATIONAL_TRANSFER, amount, from_account,
        )
        compliance_total = sum(Decimal(str(c.calculate(amount))) for c in comp)
        for c in comp:
            fees_lines.append(
                {
                    'code': f'compliance:{c.code}',
                    'label': c.name,
                    'amount': str(c.calculate(amount)),
                    'line_kind': 'compliance',
                },
            )
        requires_regulated_session = True

    return {
        'amount': str(amount),
        'currency': from_account.currency.code,
        'fee_total': str(fee),
        'base_transfer_fee': str(base_transfer_fee),
        'total_debit': str(total_debit),
        'compliance_fee_total': str(compliance_total),
        'fees': fees_lines,
        'exchange_rate': str(exchange_rate),
        'credited_amount': str(credited_amount),
        'credited_currency': from_account.currency.code,
        'destination': {
            'account_type': 'EXTERNAL' if tx_type == Transaction.TransactionType.TRANSFER_EXTERNAL else 'ACCOUNT',
            'last_four': to_account_number[-4:],
            'account_number': to_account_number,
        },
        'requires_otp': requires_otp,
        'requires_regulated_session': requires_regulated_session,
        'charge_upfront': bool(fee_row.charge_upfront) if fee_row else True,
        'fee_billing': 'upfront' if (not fee_row or fee_row.charge_upfront) else 'net_of_recipient',
    }


def _outbound_same_currency_pricing(
    from_account: Account,
    amount: Decimal,
    tx_type: str,
) -> tuple[Decimal, Decimal, Decimal, TransactionFee | None]:
    """Fee and total debit when beneficiary is off-platform (no credit account)."""
    fee_type = _transfer_fee_type(tx_type)
    fee = _get_fee(fee_type, amount)
    fee_row = _get_fee_row(fee_type)
    if fee_row and not fee_row.charge_upfront:
        total_debit = amount
    else:
        total_debit = amount + fee
    if total_debit < 0:
        raise TransactionError('Invalid transfer amount.')
    return fee, total_debit, Decimal('1'), fee_row


@db_transaction.atomic
def record_outbound_transfer(
    from_account_id: str,
    destination_account_number: str,
    amount: Decimal,
    description: str,
    initiated_by,
    tx_type: str,
    idempotency_key: str | None = None,
    recipient_metadata: dict | None = None,
    international_wire_details: dict | None = None,
) -> Transaction:
    """
    Debit the sender and record the transfer. Beneficiary details live in metadata;
    no SafaPay Bank recipient account is required or credited.
    """
    if idempotency_key:
        existing = Transaction.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing

    amount = Decimal(str(amount))
    if amount <= 0:
        raise TransactionError('Amount must be positive.')

    dest_number = normalize_destination_account_number(destination_account_number)
    from_account = Account.objects.select_for_update().get(id=from_account_id)

    if not from_account.is_active:
        raise AccountStatusError('Source account is not active.')
    if from_account.account_number == dest_number:
        raise TransactionError('Source and destination accounts must be different.')

    fee, total_debit, exchange_rate, fee_row = _outbound_same_currency_pricing(
        from_account,
        amount,
        tx_type,
    )

    if from_account.available_balance < total_debit:
        raise InsufficientFundsError('Insufficient available balance.')

    metadata = dict(recipient_metadata or {})
    metadata['destination_account_number'] = dest_number
    metadata['outbound_transfer'] = True
    if international_wire_details:
        metadata['international_wire'] = dict(international_wire_details)

    user_memo = (description or '').strip()
    if user_memo.lower() in ('', 'transfer'):
        user_memo = ''
    holder = (metadata.get('recipient_account_holder_name') or '').strip()
    bank = (metadata.get('external_bank_name') or '').strip()
    system = build_transfer_out_system_narration(
        tx_type,
        beneficiary_name=holder,
        bank_name=bank,
        destination_account_number=dest_number,
    )
    metadata['system_narration'] = system

    tx = Transaction.objects.create(
        transaction_type=tx_type,
        amount=amount,
        currency=from_account.currency.code,
        from_account=from_account,
        to_account=None,
        status=Transaction.Status.COMPLETED,
        description='Transfer',
        fee_amount=fee,
        exchange_rate=exchange_rate,
        initiated_by=initiated_by,
        idempotency_key=idempotency_key,
        completed_at=timezone.now(),
        metadata=metadata,
    )
    finalize_transaction_description(tx, system, user_memo)

    from_account.balance -= total_debit
    from_account.available_balance -= total_debit
    from_account.save(update_fields=['balance', 'available_balance', 'updated_at'])

    if fee > 0 and fee_row:
        _record_fee(from_account, fee, tx, initiated_by)

    return tx


def build_transfer_recipient_metadata(
    *,
    transfer_type: str,
    to_account_number: str | None = None,
    account_holder_name: str | None = None,
    external_bank_name: str | None = None,
) -> dict:
    meta: dict = {}
    if to_account_number:
        meta['destination_account_number'] = to_account_number
    if account_holder_name:
        meta['recipient_account_holder_name'] = account_holder_name
    if transfer_type == Transaction.TransactionType.TRANSFER_EXTERNAL and external_bank_name:
        meta['external_bank_name'] = external_bank_name
    return meta


def _get_exchange_rate(from_currency: str, to_currency: str) -> Decimal:
    if from_currency == to_currency:
        return Decimal('1')
    try:
        rate = ExchangeRate.objects.filter(
            from_currency=from_currency, to_currency=to_currency
        ).latest('fetched_at')
        return rate.rate
    except ExchangeRate.DoesNotExist:
        raise TransactionError(f'Exchange rate not found: {from_currency}/{to_currency}')


def _get_exchange_rate_for_preview(from_currency: str, to_currency: str) -> Decimal:
    """Preview-only: same currency or missing rate table entry falls back to 1:1."""
    if from_currency == to_currency:
        return Decimal('1')
    try:
        return _get_exchange_rate(from_currency, to_currency)
    except TransactionError:
        return Decimal('1')


from .deposit_source import DepositMethod, build_deposit_narration, normalize_deposit_source
from .booking import apply_transaction_booking, apply_transaction_booking_with_fees
from .narration import (
    build_deposit_system_narration,
    build_fee_system_narration,
    build_reversal_system_narration,
    build_transfer_out_system_narration,
    finalize_transaction_description,
    normalize_deposit_source_optional,
)

# Admin outbound placeholder when beneficiary account number omitted (16 digits).
ADMIN_OUTBOUND_PLACEHOLDER_ACCOUNT = '9999999999999999'


def preview_deposit(amount: Decimal) -> dict:
    amount = Decimal(str(amount))
    if amount <= 0:
        raise TransactionError('Amount must be positive.')
    fee = _get_fee(TransactionFee.FeeType.DEPOSIT, amount)
    net_amount = amount - fee
    return {
        'amount': str(amount),
        'fee': str(fee),
        'net_credit': str(net_amount),
    }


def _mirror_failed_deposit_lines(
    account: Account,
    deposit_tx: Transaction,
    fee: Decimal,
    initiated_by,
    deposit_method: str,
    deposit_source: dict,
    deposit_narration: str,
) -> list[Transaction]:
    """Failed deposits always pair with reversal lines (principal + fee); no balance movement."""
    base_meta = {
        'failed_deposit_mirror': True,
        'deposit_method': deposit_method,
        'deposit_source': deposit_source,
        'parent_deposit_id': str(deposit_tx.id),
    }
    related: list[Transaction] = []

    rev_system = build_reversal_system_narration(
        deposit_tx.reference_number,
        deposit_tx.description,
    )
    principal_rev = Transaction.objects.create(
        transaction_type=Transaction.TransactionType.REVERSAL,
        amount=deposit_tx.amount,
        currency=account.currency.code,
        from_account=account,
        status=Transaction.Status.FAILED,
        description='Reversal',
        fee_amount=Decimal('0'),
        initiated_by=initiated_by,
        original_transaction=deposit_tx,
        metadata={
            **base_meta,
            'mirror_kind': 'principal_reversal',
            'deposit_narration': rev_system,
        },
    )
    finalize_transaction_description(principal_rev, rev_system)
    related.append(principal_rev)

    if fee > 0:
        fee_system = build_fee_system_narration(deposit_tx.reference_number)
        fee_tx = Transaction.objects.create(
            transaction_type=Transaction.TransactionType.FEE,
            amount=fee,
            currency=account.currency.code,
            from_account=account,
            status=Transaction.Status.FAILED,
            description='Service fee',
            fee_amount=Decimal('0'),
            initiated_by=initiated_by,
            original_transaction=deposit_tx,
            metadata={
                **base_meta,
                'mirror_kind': 'fee',
                'deposit_narration': fee_system,
            },
        )
        finalize_transaction_description(fee_tx, fee_system)
        fee_rev_system = build_reversal_system_narration(
            fee_tx.reference_number,
            fee_system,
        )
        fee_rev = Transaction.objects.create(
            transaction_type=Transaction.TransactionType.REVERSAL,
            amount=fee,
            currency=account.currency.code,
            to_account=account,
            status=Transaction.Status.FAILED,
            description='Reversal',
            fee_amount=Decimal('0'),
            initiated_by=initiated_by,
            original_transaction=fee_tx,
            metadata={
                **base_meta,
                'mirror_kind': 'fee_reversal',
                'deposit_narration': fee_rev_system,
            },
        )
        finalize_transaction_description(fee_rev, fee_rev_system)
        related.extend([fee_tx, fee_rev])

    return related


@db_transaction.atomic
def admin_deposit(
    account_id: str,
    amount: Decimal,
    description: str,
    initiated_by,
    *,
    deposit_method: str = DepositMethod.TRANSFER,
    status: str | None = None,
    audit_note: str = '',
    deposit_source: dict | None = None,
    idempotency_key: str | None = None,
    transaction_at=None,
) -> tuple[Transaction, list[Transaction]]:
    if idempotency_key:
        existing = Transaction.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing, []

    amount = Decimal(str(amount))
    if amount <= 0:
        raise TransactionError('Amount must be positive.')

    valid_methods = {c[0] for c in DepositMethod.CHOICES}
    if deposit_method not in valid_methods:
        raise TransactionError('Invalid deposit method.')

    tx_status = str(status or Transaction.Status.COMPLETED).upper()
    valid_statuses = {c[0] for c in Transaction.Status.choices}
    if tx_status not in valid_statuses:
        raise TransactionError('Invalid transaction status.')

    account = Account.objects.select_for_update().get(id=account_id)
    if not account.is_active:
        raise AccountStatusError('Account is not active.')

    fee = _get_fee(TransactionFee.FeeType.DEPOSIT, amount)
    credit_balance = tx_status == Transaction.Status.COMPLETED
    net_amount = amount - fee if credit_balance else Decimal('0')

    source = normalize_deposit_source_optional(deposit_method, deposit_source)
    system_narration = build_deposit_system_narration(deposit_method, source)
    legacy_narration = build_deposit_narration(deposit_method, source)
    user_memo = (description or '').strip()
    if user_memo.lower() in ('', 'deposit'):
        user_memo = ''

    from .admin_transaction import build_admin_deposit_audit_note

    if not (audit_note or '').strip():
        audit_note = build_admin_deposit_audit_note(
            account, deposit_method, tx_status, amount, source, initiated_by,
        )

    metadata = {
        'deposit_method': deposit_method,
        'deposit_source': source,
        'deposit_narration': legacy_narration,
        'system_narration': system_narration,
        'admin_deposit': True,
        'admin_note': audit_note,
    }

    tx = Transaction.objects.create(
        transaction_type=Transaction.TransactionType.DEPOSIT,
        amount=amount,
        currency=account.currency.code,
        to_account=account,
        status=tx_status,
        description='Deposit',
        fee_amount=fee,
        initiated_by=initiated_by,
        idempotency_key=idempotency_key,
        completed_at=None,
        metadata=metadata,
    )
    finalize_transaction_description(tx, system_narration, user_memo)

    related: list[Transaction] = []
    if tx_status == Transaction.Status.FAILED:
        related = _mirror_failed_deposit_lines(
            account, tx, fee, initiated_by, deposit_method, source, system_narration,
        )

    if transaction_at is not None:
        apply_transaction_booking(tx, transaction_at, settled=credit_balance)

    if credit_balance:
        account.balance += net_amount
        account.available_balance += net_amount
        account.save(update_fields=['balance', 'available_balance', 'updated_at'])
        if fee > 0:
            _record_fee(account, fee, tx, initiated_by)

    if transaction_at is not None:
        for rel in related:
            apply_transaction_booking(rel, transaction_at, settled=False)

    return tx, related


@db_transaction.atomic
def admin_account_debit(
    from_account_id: str,
    amount: Decimal,
    transfer_type: str,
    initiated_by,
    *,
    description: str = '',
    transaction_at=None,
    status: str | None = None,
    to_account_id: str | None = None,
    beneficiary_name: str = '',
    bank_name: str = '',
    account_number: str = '',
    international_details: dict | None = None,
    idempotency_key: str | None = None,
) -> Transaction:
    """Admin-initiated outbound transfer (internal / external / international)."""
    if idempotency_key:
        existing = Transaction.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing

    amount = Decimal(str(amount))
    if amount <= 0:
        raise TransactionError('Amount must be positive.')

    valid_types = {
        Transaction.TransactionType.TRANSFER_INTERNAL,
        Transaction.TransactionType.TRANSFER_EXTERNAL,
        Transaction.TransactionType.TRANSFER_INTERNATIONAL,
    }
    tx_type = str(transfer_type).upper()
    if tx_type not in valid_types:
        raise TransactionError('Invalid transfer type.')

    tx_status = str(status or Transaction.Status.COMPLETED).upper()
    valid_statuses = {c[0] for c in Transaction.Status.choices}
    if tx_status not in valid_statuses:
        raise TransactionError('Invalid transaction status.')

    user_memo = (description or '').strip()
    if user_memo.lower() in ('', 'transfer', 'withdrawal'):
        user_memo = ''

    credit_balance = tx_status == Transaction.Status.COMPLETED

    internal_dest = (to_account_id or account_number or '').strip()
    if tx_type == Transaction.TransactionType.TRANSFER_INTERNAL and internal_dest:
        to_account = resolve_account_by_identifier(internal_dest)
        if not to_account:
            raise TransactionError('Destination account not found.')
        if str(to_account.id) == str(from_account_id):
            raise TransactionError('Source and destination accounts must be different.')

        holder = (beneficiary_name or '').strip() or (
            getattr(to_account.owner, 'full_name', None) or getattr(to_account.owner, 'email', '')
        )
        system = build_transfer_out_system_narration(
            tx_type,
            beneficiary_name=holder,
            to_account_number=to_account.account_number,
        )
        recipient_meta = {
            'recipient_account_holder_name': holder,
            'destination_account_number': to_account.account_number,
            'admin_debit': True,
        }
        recipient_meta['system_narration'] = system

        if credit_balance:
            tx = transfer(
                from_account_id,
                str(to_account.id),
                amount,
                user_memo,
                initiated_by,
                tx_type=tx_type,
                idempotency_key=idempotency_key,
                recipient_metadata=recipient_meta,
            )
            finalize_transaction_description(tx, system, user_memo)
        else:
            from_account = Account.objects.select_for_update().get(id=from_account_id)
            if not from_account.is_active:
                raise AccountStatusError('Source account is not active.')
            fee = _get_fee(_transfer_fee_type(tx_type), amount)
            tx = Transaction.objects.create(
                transaction_type=tx_type,
                amount=amount,
                currency=from_account.currency.code,
                from_account=from_account,
                to_account=to_account,
                status=tx_status,
                description='Transfer',
                fee_amount=fee,
                exchange_rate=Decimal('1'),
                initiated_by=initiated_by,
                idempotency_key=idempotency_key,
                metadata=recipient_meta,
            )
            finalize_transaction_description(tx, system, user_memo)
    else:
        dest_raw = (account_number or '').strip()
        if dest_raw:
            dest_number = normalize_destination_account_number(dest_raw)
        else:
            dest_number = ADMIN_OUTBOUND_PLACEHOLDER_ACCOUNT

        recipient_meta = build_transfer_recipient_metadata(
            transfer_type=tx_type,
            to_account_number=dest_number,
            account_holder_name=beneficiary_name,
            external_bank_name=bank_name,
        )
        recipient_meta['admin_debit'] = True
        intl_wire = None
        if tx_type == Transaction.TransactionType.TRANSFER_INTERNATIONAL and international_details:
            intl_wire = {k: v for k, v in international_details.items() if v}

        system = build_transfer_out_system_narration(
            tx_type,
            beneficiary_name=beneficiary_name,
            bank_name=bank_name,
            destination_account_number=dest_number,
        )

        if not credit_balance:
            from_account = Account.objects.select_for_update().get(id=from_account_id)
            metadata = dict(recipient_meta)
            metadata['destination_account_number'] = dest_number
            metadata['outbound_transfer'] = True
            metadata['system_narration'] = system
            if intl_wire:
                metadata['international_wire'] = intl_wire
            tx = Transaction.objects.create(
                transaction_type=tx_type,
                amount=amount,
                currency=from_account.currency.code,
                from_account=from_account,
                to_account=None,
                status=tx_status,
                description='Transfer',
                fee_amount=Decimal('0'),
                exchange_rate=Decimal('1'),
                initiated_by=initiated_by,
                idempotency_key=idempotency_key,
                metadata=metadata,
            )
            finalize_transaction_description(tx, system, user_memo)
        else:
            tx = record_outbound_transfer(
                from_account_id,
                dest_number,
                amount,
                user_memo,
                initiated_by,
                tx_type=tx_type,
                idempotency_key=idempotency_key,
                recipient_metadata=recipient_meta,
                international_wire_details=intl_wire,
            )
            meta = dict(tx.metadata or {})
            meta['system_narration'] = system
            tx.metadata = meta
            tx.save(update_fields=['metadata'])
            finalize_transaction_description(tx, system, user_memo)

    if transaction_at is not None:
        apply_transaction_booking_with_fees(tx, transaction_at, settled=credit_balance)

    return tx


@db_transaction.atomic
def deposit(account_id: str, amount: Decimal, description: str, initiated_by, idempotency_key: str = None) -> Transaction:
    tx, _related = admin_deposit(
        account_id,
        amount,
        description,
        initiated_by,
        deposit_method=DepositMethod.TRANSFER,
        status=Transaction.Status.COMPLETED,
        deposit_source={'depositor_name': 'Customer', 'sender_bank_name': 'N/A', 'sender_account_number': 'N/A'},
        idempotency_key=idempotency_key,
    )
    return tx


@db_transaction.atomic
def withdraw(
    account_id: str,
    amount: Decimal,
    description: str,
    initiated_by,
    idempotency_key: str = None,
    additional_fee: Decimal = None,
) -> Transaction:
    if idempotency_key:
        existing = Transaction.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing

    amount = Decimal(str(amount))
    if amount <= 0:
        raise TransactionError('Amount must be positive.')

    extra = Decimal(str(additional_fee or '0'))
    if extra < 0:
        raise TransactionError('Additional fee cannot be negative.')

    account = Account.objects.select_for_update().get(id=account_id)
    if not account.is_active:
        raise AccountStatusError('Account is not active.')

    fee = _get_fee(TransactionFee.FeeType.WITHDRAWAL, amount) + extra
    total_debit = amount + fee

    if account.available_balance < total_debit:
        raise InsufficientFundsError('Insufficient available balance.')

    tx = Transaction.objects.create(
        transaction_type=Transaction.TransactionType.WITHDRAWAL,
        amount=amount,
        currency=account.currency.code,
        from_account=account,
        status=Transaction.Status.COMPLETED,
        description=description or 'Withdrawal',
        fee_amount=fee,
        initiated_by=initiated_by,
        idempotency_key=idempotency_key,
        completed_at=timezone.now(),
    )

    account.balance -= total_debit
    account.available_balance -= total_debit
    account.save(update_fields=['balance', 'available_balance', 'updated_at'])

    if fee > 0:
        _record_fee(account, fee, tx, initiated_by)

    return tx


@db_transaction.atomic
def transfer(
    from_account_id: str,
    to_account_id: str,
    amount: Decimal,
    description: str,
    initiated_by,
    tx_type: str = Transaction.TransactionType.TRANSFER_INTERNAL,
    idempotency_key: str = None,
    skip_international_aggregate_fee: bool = False,
    international_wire_details: dict | None = None,
    recipient_metadata: dict | None = None,
) -> Transaction:
    if idempotency_key:
        existing = Transaction.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing

    amount = Decimal(str(amount))
    if amount <= 0:
        raise TransactionError('Amount must be positive.')

    from_account = Account.objects.select_for_update().get(id=from_account_id)
    to_account = Account.objects.select_for_update().get(id=to_account_id)

    if not from_account.is_active:
        raise AccountStatusError('Source account is not active.')
    if not to_account.is_active:
        raise AccountStatusError('Destination account is not active.')

    fee_type = (
        TransactionFee.FeeType.TRANSFER_INTERNATIONAL
        if tx_type == Transaction.TransactionType.TRANSFER_INTERNATIONAL
        else TransactionFee.FeeType.TRANSFER_LOCAL
    )
    if skip_international_aggregate_fee and tx_type == Transaction.TransactionType.TRANSFER_INTERNATIONAL:
        fee = Decimal('0')
        fee_row = None
    else:
        fee = _get_fee(fee_type, amount)
        fee_row = _get_fee_row(fee_type)
    same_ccy = from_account.currency.code == to_account.currency.code

    exchange_rate = _get_exchange_rate(
        from_account.currency.code,
        to_account.currency.code,
    )
    if fee_row and not fee_row.charge_upfront and same_ccy:
        total_debit = amount
        credited_amount = amount * exchange_rate - fee
    else:
        total_debit = amount + fee
        credited_amount = amount * exchange_rate

    if credited_amount < 0:
        raise TransactionError('Transfer amount must be greater than the fee for this pricing mode.')

    if from_account.available_balance < total_debit:
        raise InsufficientFundsError('Insufficient available balance.')

    metadata = dict(recipient_metadata or {})
    if tx_type == Transaction.TransactionType.TRANSFER_INTERNATIONAL and international_wire_details:
        metadata['international_wire'] = dict(international_wire_details)

    user_memo = (description or '').strip()
    if user_memo.lower() in ('', 'transfer'):
        user_memo = ''
    holder = ''
    if recipient_metadata:
        holder = (recipient_metadata.get('recipient_account_holder_name') or '').strip()
    if not holder and to_account.owner:
        holder = (getattr(to_account.owner, 'full_name', None) or getattr(to_account.owner, 'email', '') or '').strip()
    bank = (metadata.get('external_bank_name') or '').strip() if metadata else ''
    system = build_transfer_out_system_narration(
        tx_type,
        beneficiary_name=holder,
        bank_name=bank,
        to_account_number=to_account.account_number,
    )
    if metadata is None:
        metadata = {}
    metadata = dict(metadata)
    metadata['system_narration'] = system

    tx = Transaction.objects.create(
        transaction_type=tx_type,
        amount=amount,
        currency=from_account.currency.code,
        from_account=from_account,
        to_account=to_account,
        status=Transaction.Status.COMPLETED,
        description='Transfer',
        fee_amount=fee,
        exchange_rate=exchange_rate,
        initiated_by=initiated_by,
        idempotency_key=idempotency_key,
        completed_at=timezone.now(),
        metadata=metadata,
    )
    finalize_transaction_description(tx, system, user_memo)

    from_account.balance -= total_debit
    from_account.available_balance -= total_debit
    from_account.save(update_fields=['balance', 'available_balance', 'updated_at'])

    to_account.balance += credited_amount
    to_account.available_balance += credited_amount
    to_account.save(update_fields=['balance', 'available_balance', 'updated_at'])

    if fee > 0:
        if fee_row and not fee_row.charge_upfront and same_ccy:
            _record_fee(to_account, fee, tx, initiated_by)
        else:
            _record_fee(from_account, fee, tx, initiated_by)

    return tx


def _transfer_pricing(
    from_account: Account,
    to_account: Account,
    amount: Decimal,
    tx_type: str,
    skip_international_aggregate_fee: bool = False,
) -> tuple[Decimal, Decimal, Decimal, Decimal, TransactionFee | None, bool]:
    """Returns fee, total_debit, exchange_rate, credited_amount, fee_row, same_ccy."""
    fee_type = (
        TransactionFee.FeeType.TRANSFER_INTERNATIONAL
        if tx_type == Transaction.TransactionType.TRANSFER_INTERNATIONAL
        else TransactionFee.FeeType.TRANSFER_LOCAL
    )
    if skip_international_aggregate_fee and tx_type == Transaction.TransactionType.TRANSFER_INTERNATIONAL:
        fee = Decimal('0')
        fee_row = None
    else:
        fee = _get_fee(fee_type, amount)
        fee_row = _get_fee_row(fee_type)
    same_ccy = from_account.currency.code == to_account.currency.code
    exchange_rate = _get_exchange_rate(from_account.currency.code, to_account.currency.code)
    if fee_row and not fee_row.charge_upfront and same_ccy:
        total_debit = amount
        credited_amount = amount * exchange_rate - fee
    else:
        total_debit = amount + fee
        credited_amount = amount * exchange_rate
    if credited_amount < 0:
        raise TransactionError('Transfer amount must be greater than the fee for this pricing mode.')
    return fee, total_debit, exchange_rate, credited_amount, fee_row, same_ccy


@db_transaction.atomic
def create_pending_international_transfer(
    from_account_id: str,
    amount: Decimal,
    description: str,
    initiated_by,
    destination_account_number: str,
    tx_type: str = Transaction.TransactionType.TRANSFER_INTERNATIONAL,
    idempotency_key: str | None = None,
    international_wire_details: dict | None = None,
    recipient_metadata: dict | None = None,
) -> Transaction:
    """
    Debit principal + transfer fee immediately; mark PENDING until compliance completes.
    Beneficiary is recorded in metadata (no in-platform credit account).
    """
    if idempotency_key:
        existing = Transaction.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing

    amount = Decimal(str(amount))
    if amount <= 0:
        raise TransactionError('Amount must be positive.')

    dest_number = normalize_destination_account_number(destination_account_number)
    from_account = Account.objects.select_for_update().get(id=from_account_id)

    if not from_account.is_active:
        raise AccountStatusError('Source account is not active.')
    if from_account.account_number == dest_number:
        raise TransactionError('Source and destination accounts must be different.')

    fee, total_debit, exchange_rate, fee_row = _outbound_same_currency_pricing(
        from_account,
        amount,
        tx_type,
    )

    if from_account.available_balance < total_debit:
        raise InsufficientFundsError('Insufficient available balance.')

    metadata = dict(recipient_metadata or {})
    metadata['destination_account_number'] = dest_number
    metadata['awaiting_compliance'] = True
    metadata['outbound_transfer'] = True
    if international_wire_details:
        metadata['international_wire'] = dict(international_wire_details)

    tx = Transaction.objects.create(
        transaction_type=tx_type,
        amount=amount,
        currency=from_account.currency.code,
        from_account=from_account,
        to_account=None,
        status=Transaction.Status.PENDING,
        description=description or 'International transfer',
        fee_amount=fee,
        exchange_rate=exchange_rate,
        initiated_by=initiated_by,
        idempotency_key=idempotency_key,
        completed_at=None,
        metadata=metadata,
    )

    from_account.balance -= total_debit
    from_account.available_balance -= total_debit
    from_account.save(update_fields=['balance', 'available_balance', 'updated_at'])

    if fee > 0 and fee_row:
        _record_fee(from_account, fee, tx, initiated_by)

    return tx


@db_transaction.atomic
def complete_pending_international_transfer(transaction_id: str, initiated_by) -> Transaction:
    """Credit beneficiary and mark PENDING international transfer COMPLETED."""
    tx = Transaction.objects.select_for_update().get(id=transaction_id)

    if tx.status == Transaction.Status.COMPLETED:
        return tx
    if tx.status != Transaction.Status.PENDING:
        raise TransactionError('Transfer is not awaiting completion.')

    metadata = dict(tx.metadata or {})
    metadata.pop('awaiting_compliance', None)

    if tx.to_account_id:
        credited_amount = Decimal(str(metadata.get('pending_credited_amount', tx.amount)))
        to_account = Account.objects.select_for_update().get(id=tx.to_account_id)
        to_account.balance += credited_amount
        to_account.available_balance += credited_amount
        to_account.save(update_fields=['balance', 'available_balance', 'updated_at'])
    tx.metadata = metadata
    tx.status = Transaction.Status.COMPLETED
    tx.completed_at = timezone.now()
    tx.save(update_fields=['status', 'completed_at', 'metadata'])

    return tx


@db_transaction.atomic
def reverse_transaction(transaction_id: str, reversed_by) -> Transaction:
    original = Transaction.objects.select_for_update().get(id=transaction_id)

    if original.status != Transaction.Status.COMPLETED:
        raise TransactionError('Only completed transactions can be reversed.')
    if original.reversals.exists():
        raise TransactionError('Transaction has already been reversed.')

    rev_system = build_reversal_system_narration(original.reference_number, original.description)
    reversal = Transaction.objects.create(
        transaction_type=Transaction.TransactionType.REVERSAL,
        amount=original.amount,
        currency=original.currency,
        from_account=original.to_account,
        to_account=original.from_account,
        status=Transaction.Status.COMPLETED,
        description='Reversal',
        fee_amount=0,
        initiated_by=reversed_by,
        reversed_by=reversed_by,
        original_transaction=original,
        completed_at=timezone.now(),
    )
    finalize_transaction_description(reversal, rev_system)

    if original.to_account:
        acc = Account.objects.select_for_update().get(id=original.to_account.id)
        acc.balance -= original.amount
        acc.available_balance -= original.amount
        acc.save(update_fields=['balance', 'available_balance', 'updated_at'])

    if original.from_account:
        acc = Account.objects.select_for_update().get(id=original.from_account.id)
        acc.balance += original.amount + original.fee_amount
        acc.available_balance += original.amount + original.fee_amount
        acc.save(update_fields=['balance', 'available_balance', 'updated_at'])

    original.status = Transaction.Status.REVERSED
    original.save(update_fields=['status'])

    return reversal


def _record_fee(account, fee_amount, parent_tx, initiated_by):
    fee_system = build_fee_system_narration(parent_tx.reference_number)
    settled = parent_tx.status == Transaction.Status.COMPLETED
    fee_tx = Transaction.objects.create(
        transaction_type=Transaction.TransactionType.FEE,
        amount=fee_amount,
        currency=account.currency.code,
        from_account=account,
        status=parent_tx.status,
        description='Service fee',
        fee_amount=0,
        initiated_by=initiated_by,
        metadata={
            'parent_transaction_id': str(parent_tx.id),
            'fee_for_reference': parent_tx.reference_number,
        },
    )
    finalize_transaction_description(fee_tx, fee_system)
    booked_at = parent_tx.completed_at or parent_tx.created_at
    if booked_at:
        apply_transaction_booking(fee_tx, booked_at, settled=settled)
