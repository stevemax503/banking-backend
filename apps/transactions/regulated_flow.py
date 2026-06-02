"""Session-based compliance fees: charge each line, then one OTP per line, then complete transfer/payout."""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import Max
from django.utils import timezone

from apps.accounts.models import Account
from apps.users.email_otp import create_email_otp
from apps.users.models import EmailOTPToken
from apps.notifications.services import queue_email_notification

from .models import Transaction
from .regulated_models import ComplianceFeeLine, RegulatedTransferSession, RegulatedTransferSessionLine


PURPOSE_REGULATED_FEE = 'regulated_fee'

logger = logging.getLogger(__name__)

SESSION_TTL = timedelta(minutes=45)


def _queue_compliance_fee_otp_email(user, code: str, fee_name: str) -> None:
    """Queue OTP email without failing the compliance charge if SMTP/Celery is unavailable."""
    try:
        queue_email_notification(
            str(user.id),
            'compliance_fee_otp',
            {
                'otp': code,
                'full_name': user.full_name,
                'fee_name': fee_name,
                'valid_hours': 48,
            },
        )
    except Exception:
        logger.exception('Compliance fee OTP email could not be queued for user %s', user.id)


def _queue_compliance_payment_confirmed_notification(user, fee_name: str) -> None:
    try:
        queue_email_notification(
            str(user.id),
            'compliance_payment_confirmed',
            {'full_name': user.full_name, 'fee_name': fee_name},
        )
    except Exception:
        logger.exception('Compliance payment confirmed notification failed for user %s', user.id)

_ACTIVE_SESSION_STATUSES = (
    RegulatedTransferSession.Status.PENDING,
    RegulatedTransferSession.Status.IN_PROGRESS,
    RegulatedTransferSession.Status.LINES_VERIFIED,
)


class RegulatedFlowError(Exception):
    pass


def _flow_matches_line(line: ComplianceFeeLine, flow: str) -> bool:
    if line.applies_to == ComplianceFeeLine.AppliesTo.BOTH:
        return True
    return line.applies_to == flow


def _resolve_compliance_user(account: Account | None, user=None):
    if user is not None:
        return user
    if account is not None:
        return account.owner
    return None


def is_compliance_fees_exempt(user) -> bool:
    """True when admin has exempted this customer from all compliance fee lines."""
    if user is None:
        return False
    return bool(getattr(user, 'compliance_fees_exempt', False))


def applicable_compliance_lines(
    flow: str,
    principal: Decimal,
    account: Account | None = None,
    user=None,
) -> list[ComplianceFeeLine]:
    """
    Return compliance fee lines for a flow and principal amount.
    Per-user lines for this flow take precedence; otherwise global lines for this flow are used.
    Exempt customers receive no lines (international transfer and loan payout proceed without compliance).
    """
    customer = _resolve_compliance_user(account, user)
    if customer is not None and is_compliance_fees_exempt(customer):
        return []
    p = Decimal(str(principal))
    base_qs = ComplianceFeeLine.objects.filter(is_active=True, min_principal_threshold__lte=p).order_by(
        'sort_order', 'name',
    )

    if customer is not None:
        user_lines = [x for x in base_qs.filter(user=customer) if _flow_matches_line(x, flow)]
        if user_lines:
            return user_lines

    return [x for x in base_qs.filter(user__isnull=True) if _flow_matches_line(x, flow)]


def resolve_compliance_scope_at_start(
    flow: str,
    principal: Decimal,
    account: Account | None = None,
    user=None,
) -> str:
    """GLOBAL if session is built from global fee lines; PERSONAL if from per-user lines."""
    customer = _resolve_compliance_user(account, user)
    p = Decimal(str(principal))
    base_qs = ComplianceFeeLine.objects.filter(is_active=True, min_principal_threshold__lte=p)
    if customer is not None:
        user_lines = [x for x in base_qs.filter(user=customer) if _flow_matches_line(x, flow)]
        if user_lines:
            return RegulatedTransferSession.ComplianceScope.PERSONAL
    return RegulatedTransferSession.ComplianceScope.GLOBAL


def applicable_compliance_lines_for_session_sync(
    session: RegulatedTransferSession,
) -> list[ComplianceFeeLine]:
    """
    New fee definitions that may be appended to an open session (same scope only).
    Global sessions only pick up new global lines; personal sessions only new personal lines.
    """
    flow = session.flow
    p = Decimal(str(session.principal_amount))
    base_qs = ComplianceFeeLine.objects.filter(
        is_active=True,
        min_principal_threshold__lte=p,
    ).order_by('sort_order', 'name')

    scope = session.compliance_scope or RegulatedTransferSession.ComplianceScope.GLOBAL
    if scope == RegulatedTransferSession.ComplianceScope.PERSONAL:
        return [x for x in base_qs.filter(user=session.user_id) if _flow_matches_line(x, flow)]
    return [x for x in base_qs.filter(user__isnull=True) if _flow_matches_line(x, flow)]


def international_requires_regulated_session(
    principal: Decimal,
    account: Account | None = None,
    user=None,
) -> bool:
    return bool(
        applicable_compliance_lines(
            RegulatedTransferSession.Flow.INTERNATIONAL_TRANSFER, principal, account, user=user,
        ),
    )


def loan_payout_requires_regulated_session(
    principal: Decimal,
    account: Account | None = None,
    user=None,
) -> bool:
    return bool(applicable_compliance_lines(RegulatedTransferSession.Flow.LOAN_PAYOUT, principal, account, user=user))


def _refresh_pending_session_line_amounts(session: RegulatedTransferSession) -> int:
    """Recompute amounts on PENDING lines from current fee definitions (admin may have edited pricing)."""
    updated = 0
    for ln in session.lines.select_related('fee_line').filter(
        status=RegulatedTransferSessionLine.Status.PENDING,
    ):
        if not ln.fee_line.is_active:
            continue
        new_amt = ln.fee_line.calculate(session.principal_amount)
        if ln.amount != new_amt:
            ln.amount = new_amt
            ln.save(update_fields=['amount', 'updated_at'])
            updated += 1
    return updated


@db_transaction.atomic
def sync_session_compliance_lines(session: RegulatedTransferSession) -> int:
    """
    Append newly configured compliance fee lines to an active session and refresh
    pricing on existing PENDING lines. Returns the number of lines added.
    """
    if session.status not in _ACTIVE_SESSION_STATUSES:
        return 0
    if timezone.now() > session.expires_at:
        return 0

    session = (
        RegulatedTransferSession.objects.select_for_update()
        .select_related('from_account', 'user')
        .prefetch_related('lines')
        .get(pk=session.pk)
    )
    _refresh_pending_session_line_amounts(session)
    applicable = applicable_compliance_lines_for_session_sync(session)
    existing_fee_line_ids = {ln.fee_line_id for ln in session.lines.all()}
    max_seq = session.lines.aggregate(m=Max('sequence'))['m']
    next_seq = (max_seq if max_seq is not None else -1) + 1
    added = 0

    for fl in applicable:
        if fl.id in existing_fee_line_ids:
            continue
        amt = fl.calculate(session.principal_amount)
        RegulatedTransferSessionLine.objects.create(
            session=session,
            fee_line=fl,
            sequence=next_seq,
            amount=amt,
            status=RegulatedTransferSessionLine.Status.PENDING,
        )
        next_seq += 1
        added += 1

    if added and session.status == RegulatedTransferSession.Status.LINES_VERIFIED:
        session.status = RegulatedTransferSession.Status.IN_PROGRESS
        session.save(update_fields=['status', 'updated_at'])
    elif added and session.status == RegulatedTransferSession.Status.PENDING:
        session.status = RegulatedTransferSession.Status.IN_PROGRESS
        session.save(update_fields=['status', 'updated_at'])

    return added


def sync_all_active_compliance_sessions(*, user=None) -> int:
    """Sync every non-expired active session (optionally for one user). Returns total lines added."""
    now = timezone.now()
    qs = RegulatedTransferSession.objects.filter(
        status__in=_ACTIVE_SESSION_STATUSES,
        expires_at__gt=now,
    ).select_related('from_account', 'user')
    if user is not None:
        qs = qs.filter(user=user)
    total = 0
    for session in qs.iterator(chunk_size=50):
        try:
            total += sync_session_compliance_lines(session)
        except Exception:
            logger.exception('Failed to sync compliance lines for session %s', session.pk)
    return total


def _session_is_active(session: RegulatedTransferSession) -> bool:
    return session.status in _ACTIVE_SESSION_STATUSES and timezone.now() <= session.expires_at


def _find_reusable_pending_international_tx(from_account: Account, user) -> Transaction | None:
    """Pending intl transfer with no active compliance session (e.g. after admin cancelled session)."""
    for tx in (
        Transaction.objects.filter(
            from_account=from_account,
            initiated_by=user,
            status=Transaction.Status.PENDING,
            transaction_type=Transaction.TransactionType.TRANSFER_INTERNATIONAL,
        )
        .order_by('-created_at')[:20]
    ):
        meta = tx.metadata or {}
        if not meta.get('awaiting_compliance'):
            continue
        sid = meta.get('regulated_session_id')
        if not sid:
            return tx
        linked = RegulatedTransferSession.objects.filter(pk=sid).first()
        if linked is None or linked.status == RegulatedTransferSession.Status.CANCELLED:
            return tx
        if not _session_is_active(linked):
            return tx
    return None


@db_transaction.atomic
def cancel_compliance_session(session_id, *, staff_user=None) -> RegulatedTransferSession:
    """
    Admin: cancel compliance workflow only. Does not reverse/delete the pending transfer or loan.
    Customer must start a new compliance session to continue.
    """
    session = RegulatedTransferSession.objects.select_for_update().get(id=session_id)
    if session.status in (
        RegulatedTransferSession.Status.COMPLETED,
        RegulatedTransferSession.Status.CANCELLED,
    ):
        raise RegulatedFlowError('This session is already closed.')

    session.status = RegulatedTransferSession.Status.CANCELLED
    session.save(update_fields=['status', 'updated_at'])

    tx = session.transfer_transaction
    if tx:
        meta = dict(tx.metadata or {})
        meta.pop('regulated_session_id', None)
        tx.metadata = meta
        tx.save(update_fields=['metadata'])

    return session


def _attach_pending_transfer_to_session(
    session: RegulatedTransferSession,
    user,
    from_account: Account,
    destination_account_number: str,
    amount: Decimal,
    transfer_type: str,
    description: str,
    idempotency_key: str | None,
    international_wire_details: dict | None,
    recipient_metadata: dict | None = None,
) -> RegulatedTransferSession:
    if session.transfer_transaction_id:
        tx = session.transfer_transaction
        meta = dict(tx.metadata or {})
        meta['regulated_session_id'] = str(session.id)
        meta['awaiting_compliance'] = True
        tx.metadata = meta
        tx.save(update_fields=['metadata'])
        return session

    from .services import InsufficientFundsError, build_transfer_recipient_metadata, create_pending_international_transfer

    meta = recipient_metadata or build_transfer_recipient_metadata(
        transfer_type=transfer_type,
        to_account_number=destination_account_number,
    )
    pending_tx = _find_reusable_pending_international_tx(from_account, user)
    if not pending_tx:
        try:
            pending_tx = create_pending_international_transfer(
                str(from_account.id),
                amount,
                description or 'International transfer',
                user,
                destination_account_number=destination_account_number,
                tx_type=transfer_type,
                idempotency_key=idempotency_key,
                international_wire_details=international_wire_details,
                recipient_metadata=meta,
            )
        except InsufficientFundsError as e:
            raise RegulatedFlowError(str(e)) from e
    session.transfer_transaction = pending_tx
    session.save(update_fields=['transfer_transaction', 'updated_at'])
    meta = dict(pending_tx.metadata or {})
    meta['regulated_session_id'] = str(session.id)
    meta['awaiting_compliance'] = True
    pending_tx.metadata = meta
    pending_tx.save(update_fields=['metadata'])
    return session


@db_transaction.atomic
def start_international_session(
    user,
    from_account: Account,
    destination_account_number: str,
    amount: Decimal,
    transfer_type: str,
    description: str = '',
    idempotency_key: str | None = None,
    international_wire_details: dict | None = None,
    recipient_metadata: dict | None = None,
) -> RegulatedTransferSession:
    if transfer_type != Transaction.TransactionType.TRANSFER_INTERNATIONAL:
        raise RegulatedFlowError('Regulated session only applies to international transfers.')
    amount = Decimal(str(amount))
    lines = applicable_compliance_lines(
        RegulatedTransferSession.Flow.INTERNATIONAL_TRANSFER, amount, from_account, user=user,
    )
    if not lines:
        raise RegulatedFlowError('No compliance fee lines configured for this transfer.')

    scope = resolve_compliance_scope_at_start(
        RegulatedTransferSession.Flow.INTERNATIONAL_TRANSFER, amount, from_account, user=user,
    )

    if idempotency_key:
        existing = RegulatedTransferSession.objects.filter(idempotency_key=idempotency_key).first()
        if existing and _session_is_active(existing):
            if (
                international_wire_details is not None
                and existing.international_wire_details is not None
                and existing.international_wire_details != international_wire_details
            ):
                raise RegulatedFlowError(
                    'This idempotency key was already used with different international beneficiary details.',
                )
            sync_session_compliance_lines(existing)
            return _attach_pending_transfer_to_session(
                existing,
                user,
                from_account,
                destination_account_number,
                amount,
                transfer_type,
                description,
                idempotency_key,
                international_wire_details,
                recipient_metadata,
            )

    session = RegulatedTransferSession.objects.create(
        user=user,
        flow=RegulatedTransferSession.Flow.INTERNATIONAL_TRANSFER,
        status=RegulatedTransferSession.Status.PENDING,
        compliance_scope=scope,
        from_account=from_account,
        to_account=None,
        loan_application=None,
        principal_amount=amount,
        transfer_type=transfer_type,
        description=description or 'International transfer',
        international_wire_details=international_wire_details,
        idempotency_key=idempotency_key or None,
        expires_at=timezone.now() + SESSION_TTL,
    )
    for i, fl in enumerate(lines):
        amt = fl.calculate(amount)
        RegulatedTransferSessionLine.objects.create(
            session=session,
            fee_line=fl,
            sequence=i,
            amount=amt,
            status=RegulatedTransferSessionLine.Status.PENDING,
        )
    session.status = RegulatedTransferSession.Status.IN_PROGRESS
    session.save(update_fields=['status', 'updated_at'])
    return _attach_pending_transfer_to_session(
        session,
        user,
        from_account,
        destination_account_number,
        amount,
        transfer_type,
        description,
        idempotency_key,
        international_wire_details,
        recipient_metadata,
    )


@db_transaction.atomic
def start_loan_payout_session(user, from_account: Account, loan_application, idempotency_key: str | None = None):
    from apps.loans.models import LoanApplication

    if not isinstance(loan_application, LoanApplication):
        raise RegulatedFlowError('Invalid loan application.')
    if loan_application.applicant_id != user.id:
        raise RegulatedFlowError('This application does not belong to you.')
    if loan_application.status != LoanApplication.Status.APPROVED:
        raise RegulatedFlowError('Loan must be approved before payout.')
    if hasattr(loan_application, 'loan_account'):
        raise RegulatedFlowError('This loan has already been disbursed.')

    principal = Decimal(str(loan_application.requested_amount))
    lines = applicable_compliance_lines(
        RegulatedTransferSession.Flow.LOAN_PAYOUT, principal, from_account, user=user,
    )
    if not lines:
        raise RegulatedFlowError('No compliance fee lines configured for loan payout.')

    scope = resolve_compliance_scope_at_start(
        RegulatedTransferSession.Flow.LOAN_PAYOUT, principal, from_account, user=user,
    )

    if idempotency_key:
        existing = RegulatedTransferSession.objects.filter(idempotency_key=idempotency_key).first()
        if existing and _session_is_active(existing):
            sync_session_compliance_lines(existing)
            return existing

    active = get_active_loan_payout_session(loan_application, user)
    if active and timezone.now() <= active.expires_at:
        sync_session_compliance_lines(active)
        return active

    session = RegulatedTransferSession.objects.create(
        user=user,
        flow=RegulatedTransferSession.Flow.LOAN_PAYOUT,
        status=RegulatedTransferSession.Status.PENDING,
        compliance_scope=scope,
        from_account=from_account,
        to_account=None,
        loan_application=loan_application,
        principal_amount=principal,
        transfer_type='',
        description=f'Loan payout — {loan_application.product.name}',
        idempotency_key=idempotency_key or None,
        expires_at=timezone.now() + SESSION_TTL,
    )
    for i, fl in enumerate(lines):
        amt = fl.calculate(principal)
        RegulatedTransferSessionLine.objects.create(
            session=session,
            fee_line=fl,
            sequence=i,
            amount=amt,
            status=RegulatedTransferSessionLine.Status.PENDING,
        )
    session.status = RegulatedTransferSession.Status.IN_PROGRESS
    session.save(update_fields=['status', 'updated_at'])
    return session


def get_active_loan_payout_session(loan_application, user):
    return (
        RegulatedTransferSession.objects.filter(
            loan_application=loan_application,
            user=user,
            flow=RegulatedTransferSession.Flow.LOAN_PAYOUT,
            status__in=[
                RegulatedTransferSession.Status.PENDING,
                RegulatedTransferSession.Status.IN_PROGRESS,
                RegulatedTransferSession.Status.LINES_VERIFIED,
            ],
        )
        .prefetch_related('lines__fee_line')
        .order_by('-created_at')
        .first()
    )


DEFAULT_LOAN_PAYOUT_MESSAGE = 'Verification is required before we can release your funds.'

DEFAULT_COMPLIANCE_CUSTOMER_MESSAGE = 'Please wait for your bank to authorize payment.'


def generate_payment_reference(line: RegulatedTransferSessionLine) -> str:
    custom = (line.fee_line.custom_payment_reference or '').strip()
    if custom:
        return custom[:40]
    code = (line.fee_line.code or 'FEE').upper().replace('-', '')[:8]
    return f'CMP-{code}-{line.id.hex[:6].upper()}'


def compliance_payment_instructions_serialized(fee_line: ComplianceFeeLine) -> dict:
    """Customer-facing external payment config from the fee line definition."""
    usdt = {}
    if (fee_line.crypto_usdt_erc20 or '').strip():
        usdt['erc20'] = fee_line.crypto_usdt_erc20.strip()
    if (fee_line.crypto_usdt_trc20 or '').strip():
        usdt['trc20'] = fee_line.crypto_usdt_trc20.strip()
    if (fee_line.crypto_usdt_bep20 or '').strip():
        usdt['bep20'] = fee_line.crypto_usdt_bep20.strip()
    crypto = {}
    if (fee_line.crypto_btc_address or '').strip():
        crypto['btc'] = fee_line.crypto_btc_address.strip()
    if (fee_line.crypto_eth_address or '').strip():
        crypto['eth'] = fee_line.crypto_eth_address.strip()
    if usdt:
        crypto['usdt'] = usdt
    wire = {}
    if fee_line.payment_wire_enabled:
        wire = {
            'beneficiary_name': fee_line.wire_beneficiary_name.strip(),
            'bank_name': fee_line.wire_bank_name.strip(),
            'bank_address': fee_line.wire_bank_address.strip(),
            'swift_bic': fee_line.wire_swift_bic.strip(),
            'iban': fee_line.wire_iban.strip(),
            'account_number': fee_line.wire_account_number.strip(),
            'country': fee_line.wire_country.strip(),
        }
    return {
        'crypto_enabled': bool(fee_line.payment_crypto_enabled and crypto),
        'wire_enabled': bool(fee_line.payment_wire_enabled and any(wire.values())),
        'crypto': crypto,
        'wire': wire,
    }


def _assert_fee_line_payment_configured(fee_line: ComplianceFeeLine) -> None:
    instr = compliance_payment_instructions_serialized(fee_line)
    if not instr['crypto_enabled'] and not instr['wire_enabled']:
        raise RegulatedFlowError(
            'Configure crypto or wire payment details on this fee line before allowing customer payment.',
        )


def _compliance_customer_message(fee_line: ComplianceFeeLine) -> str:
    custom = (fee_line.customer_message or '').strip()
    return custom or DEFAULT_COMPLIANCE_CUSTOMER_MESSAGE


def regulated_line_generate_feedback_message(line: RegulatedTransferSessionLine) -> str:
    """Admin-configured text when the customer cannot complete Generate code."""
    fee_line = ComplianceFeeLine.objects.only('customer_message').get(pk=line.fee_line_id)
    return _compliance_customer_message(fee_line)


def loan_payout_context(loan_application, user) -> dict:
    """Customer-facing payout requirements for an approved loan application."""
    from apps.loans.models import LoanApplication

    if loan_application.status != LoanApplication.Status.APPROVED:
        return {
            'requires_compliance': False,
            'compliance_fee_total': '0',
            'fee_lines': [],
            'payout_message': '',
            'resume': None,
        }
    if hasattr(loan_application, 'loan_account'):
        return {
            'requires_compliance': False,
            'compliance_fee_total': '0',
            'fee_lines': [],
            'payout_message': '',
            'resume': None,
        }

    principal = Decimal(str(loan_application.requested_amount))
    requires = loan_payout_requires_regulated_session(principal, user=user)
    fee_lines = []
    payout_message = ''
    total = Decimal('0')
    if requires:
        applicable = applicable_compliance_lines(
            RegulatedTransferSession.Flow.LOAN_PAYOUT, principal, user=user,
        )
        for fl in applicable:
            amt = fl.calculate(principal)
            total += amt
            msg = _compliance_customer_message(fl)
            fee_lines.append({
                'code': fl.code,
                'name': fl.name,
                'amount': str(amt),
                'customer_message': msg,
            })
            if msg and not payout_message:
                payout_message = msg
        if not payout_message:
            payout_message = DEFAULT_LOAN_PAYOUT_MESSAGE

    resume = None
    session = get_active_loan_payout_session(loan_application, user)
    if session:
        if timezone.now() > session.expires_at:
            RegulatedTransferSession.objects.filter(pk=session.pk).update(
                status=RegulatedTransferSession.Status.EXPIRED,
            )
        else:
            sync_session_compliance_lines(session)
            session.refresh_from_db()
            ser = session_serialized(session)
            lines = list(session.lines.all())
            resume = {
                'session_id': ser['session_id'],
                'session_status': ser['status'],
                'from_account_id': str(session.from_account_id) if session.from_account_id else None,
                'lines_total': len(lines),
                'lines_verified': sum(
                    1 for ln in lines if ln.status == RegulatedTransferSessionLine.Status.OTP_VERIFIED
                ),
                'expires_at': ser['expires_at'],
                'can_resume': session.status in (
                    RegulatedTransferSession.Status.IN_PROGRESS,
                    RegulatedTransferSession.Status.LINES_VERIFIED,
                ),
            }

    return {
        'requires_compliance': requires,
        'compliance_fee_total': str(total),
        'fee_lines': fee_lines,
        'payout_message': payout_message if requires else '',
        'resume': resume,
    }


def assert_loan_compliance_completed_if_required(loan_application) -> None:
    """Block disbursement when compliance lines exist but session is not finished."""
    principal = Decimal(str(loan_application.requested_amount))
    if not loan_payout_requires_regulated_session(principal, user=loan_application.applicant):
        return
    completed = RegulatedTransferSession.objects.filter(
        loan_application=loan_application,
        flow=RegulatedTransferSession.Flow.LOAN_PAYOUT,
        status=RegulatedTransferSession.Status.COMPLETED,
    ).exists()
    if not completed:
        raise RegulatedFlowError(
            'Customer must complete all loan payout compliance fees before funds can be released.',
        )


def _assert_session_active(session: RegulatedTransferSession):
    if session.status not in (
        RegulatedTransferSession.Status.PENDING,
        RegulatedTransferSession.Status.IN_PROGRESS,
    ):
        raise RegulatedFlowError('This session is no longer active.')
    if timezone.now() > session.expires_at:
        RegulatedTransferSession.objects.filter(pk=session.pk).update(status=RegulatedTransferSession.Status.EXPIRED)
        raise RegulatedFlowError('This session has expired. Start again.')


@db_transaction.atomic
def allow_customer_self_charge(session_line_id) -> RegulatedTransferSessionLine:
    line = (
        RegulatedTransferSessionLine.objects.select_for_update()
        .select_related('session', 'session__from_account', 'fee_line')
        .get(id=session_line_id)
    )
    _assert_session_active(line.session)
    if line.status == RegulatedTransferSessionLine.Status.OTP_VERIFIED:
        raise RegulatedFlowError('This step is already completed.')
    _assert_fee_line_payment_configured(line.fee_line)
    line.customer_self_charge_allowed = True
    if not line.payment_reference:
        line.payment_reference = generate_payment_reference(line)
    line.save(update_fields=['customer_self_charge_allowed', 'payment_reference', 'updated_at'])
    return line


PAYMENT_PROOF_MAX_BYTES = 10 * 1024 * 1024
PAYMENT_PROOF_ALLOWED_CONTENT_TYPES = frozenset({
    'image/jpeg',
    'image/png',
    'image/webp',
    'image/gif',
    'application/pdf',
})


def _validate_payment_proof(upload) -> None:
    if upload is None:
        return
    size = getattr(upload, 'size', 0) or 0
    if size > PAYMENT_PROOF_MAX_BYTES:
        raise RegulatedFlowError('Payment proof must be 10 MB or smaller.')
    content_type = (getattr(upload, 'content_type', '') or '').split(';', 1)[0].strip().lower()
    if content_type and content_type not in PAYMENT_PROOF_ALLOWED_CONTENT_TYPES:
        raise RegulatedFlowError('Upload a JPG, PNG, WebP, GIF, or PDF receipt.')


@db_transaction.atomic
def submit_external_payment(session_line_id, user, *, payment_proof=None) -> RegulatedTransferSessionLine:
    line = (
        RegulatedTransferSessionLine.objects.select_for_update()
        .select_related('session', 'fee_line')
        .get(id=session_line_id)
    )
    if line.session.user_id != user.id:
        raise RegulatedFlowError('Access denied.')
    _assert_session_active(line.session)
    if not line.customer_self_charge_allowed:
        raise RegulatedFlowError(regulated_line_generate_feedback_message(line))
    if line.status not in (
        RegulatedTransferSessionLine.Status.PENDING,
        RegulatedTransferSessionLine.Status.PAYMENT_SUBMITTED,
    ):
        raise RegulatedFlowError('This payment step is no longer open.')
    for pl in line.session.lines.filter(sequence__lt=line.sequence).order_by('sequence'):
        if pl.status != RegulatedTransferSessionLine.Status.OTP_VERIFIED:
            raise RegulatedFlowError('Complete previous fee steps first.')
    if not line.payment_reference:
        line.payment_reference = generate_payment_reference(line)
    _validate_payment_proof(payment_proof)
    line.status = RegulatedTransferSessionLine.Status.PAYMENT_SUBMITTED
    update_fields = ['status', 'payment_reference', 'updated_at']
    if payment_proof is not None:
        line.payment_proof = payment_proof
        update_fields.append('payment_proof')
    line.save(update_fields=update_fields)
    return line


@db_transaction.atomic
def confirm_external_payment(session_line_id) -> RegulatedTransferSessionLine:
    line = (
        RegulatedTransferSessionLine.objects.select_for_update()
        .select_related('session', 'session__user', 'fee_line')
        .get(id=session_line_id)
    )
    _assert_session_active(line.session)
    if line.status == RegulatedTransferSessionLine.Status.OTP_VERIFIED:
        raise RegulatedFlowError('This step is already completed.')
    if line.status != RegulatedTransferSessionLine.Status.PAYMENT_SUBMITTED:
        raise RegulatedFlowError('The customer must confirm they have sent payment before you can verify it.')
    if not line.payment_reference:
        line.payment_reference = generate_payment_reference(line)
    line.status = RegulatedTransferSessionLine.Status.PAYMENT_CONFIRMED
    line.save(update_fields=['status', 'payment_reference', 'updated_at'])
    _queue_compliance_payment_confirmed_notification(line.session.user, line.fee_line.name)
    return line


@db_transaction.atomic
def send_compliance_line_otp(session_line_id, user, *, staff_issued: bool = False) -> RegulatedTransferSessionLine:
    """Email OTP after external payment is confirmed. No account balance debit."""
    line = (
        RegulatedTransferSessionLine.objects.select_for_update()
        .select_related('session', 'fee_line')
        .get(id=session_line_id)
    )
    session = line.session
    if session.user_id != user.id:
        raise RegulatedFlowError('Access denied.')
    _assert_session_active(session)

    for pl in session.lines.filter(sequence__lt=line.sequence).order_by('sequence'):
        if pl.status != RegulatedTransferSessionLine.Status.OTP_VERIFIED:
            raise RegulatedFlowError('Complete previous fee steps first.')

    if line.status == RegulatedTransferSessionLine.Status.OTP_VERIFIED:
        raise RegulatedFlowError('This step is already completed.')

    if line.status == RegulatedTransferSessionLine.Status.CHARGED:
        code = create_email_otp(user, PURPOSE_REGULATED_FEE, line.id)
        _queue_compliance_fee_otp_email(user, code, line.fee_line.name)
        return line

    if staff_issued:
        if line.status != RegulatedTransferSessionLine.Status.PAYMENT_CONFIRMED:
            raise RegulatedFlowError('Confirm payment received before sending a verification code.')
    else:
        raise RegulatedFlowError('Verification codes are sent by your bank after payment is confirmed.')

    line.status = RegulatedTransferSessionLine.Status.CHARGED
    line.save(update_fields=['status', 'updated_at'])

    code = create_email_otp(user, PURPOSE_REGULATED_FEE, line.id)
    _queue_compliance_fee_otp_email(user, code, line.fee_line.name)
    return line


def charge_line_and_send_otp(session_line_id, user, *, staff_issued: bool = False) -> RegulatedTransferSessionLine:
    """Backward-compatible alias for staff OTP send (external payment flow)."""
    return send_compliance_line_otp(session_line_id, user, staff_issued=staff_issued)


@db_transaction.atomic
def verify_line_otp(session_line_id, user, otp: str) -> RegulatedTransferSessionLine:
    line = RegulatedTransferSessionLine.objects.select_related('session').get(id=session_line_id)
    if line.session.user_id != user.id:
        raise RegulatedFlowError('Access denied.')
    _assert_session_active(line.session)
    if line.status != RegulatedTransferSessionLine.Status.CHARGED:
        raise RegulatedFlowError('Enter the verification code from your email.')

    otp_in = (otp or '').strip()
    if len(otp_in) != 6 or not otp_in.isdigit():
        raise RegulatedFlowError('Enter the 6-digit code from your email.')

    row = (
        EmailOTPToken.objects.filter(
            user=user,
            purpose=PURPOSE_REGULATED_FEE,
            context_id=line.id,
            token=otp_in,
            is_used=False,
            expires_at__gt=timezone.now(),
        )
        .order_by('-created_at')
        .first()
    )
    if not row:
        raise RegulatedFlowError('Invalid, expired, or already used verification code.')

    row.is_used = True
    row.save(update_fields=['is_used'])
    line.status = RegulatedTransferSessionLine.Status.OTP_VERIFIED
    line.save(update_fields=['status', 'updated_at'])

    session = line.session
    remaining = session.lines.exclude(status=RegulatedTransferSessionLine.Status.OTP_VERIFIED).exists()
    if not remaining:
        session.status = RegulatedTransferSession.Status.LINES_VERIFIED
        session.save(update_fields=['status', 'updated_at'])
    return line


def compliance_resume_for_transaction(tx: Transaction) -> dict | None:
    """Resume info for pending international transfers awaiting compliance fees."""
    if tx.status != Transaction.Status.PENDING:
        return None
    if tx.transaction_type != Transaction.TransactionType.TRANSFER_INTERNATIONAL:
        return None
    meta = tx.metadata or {}
    if not meta.get('awaiting_compliance'):
        return None

    session_id = meta.get('regulated_session_id')
    if session_id:
        session = (
            RegulatedTransferSession.objects.filter(pk=session_id)
            .prefetch_related('lines')
            .first()
        )
    else:
        session = (
            RegulatedTransferSession.objects.filter(transfer_transaction_id=tx.id)
            .exclude(status=RegulatedTransferSession.Status.CANCELLED)
            .prefetch_related('lines')
            .order_by('-created_at')
            .first()
        )
    if not session:
        return None
    if session.flow != RegulatedTransferSession.Flow.INTERNATIONAL_TRANSFER:
        return None
    if session.status in (
        RegulatedTransferSession.Status.COMPLETED,
        RegulatedTransferSession.Status.CANCELLED,
    ):
        return None

    if timezone.now() <= session.expires_at and session.status in _ACTIVE_SESSION_STATUSES:
        sync_session_compliance_lines(session)
        session.refresh_from_db()

    is_expired = timezone.now() > session.expires_at
    lines = list(session.lines.all())
    lines_verified = sum(
        1 for ln in lines if ln.status == RegulatedTransferSessionLine.Status.OTP_VERIFIED
    )
    can_resume = (
        not is_expired
        and session.status in (
            RegulatedTransferSession.Status.IN_PROGRESS,
            RegulatedTransferSession.Status.LINES_VERIFIED,
        )
    )

    return {
        'session_id': str(session.id),
        'session_status': session.status,
        'lines_total': len(lines),
        'lines_verified': lines_verified,
        'expires_at': session.expires_at.isoformat(),
        'is_expired': is_expired,
        'can_resume': can_resume,
    }


def session_serialized(session: RegulatedTransferSession) -> dict:
    lines = []
    for ln in session.lines.select_related('fee_line').order_by('sequence'):
        lines.append(
            {
                'id': str(ln.id),
                'sequence': ln.sequence,
                'name': ln.fee_line.name,
                'code': ln.fee_line.code,
                'amount': str(ln.amount),
                'status': ln.status,
                'payment_reference': ln.payment_reference or '',
                'payment_proof_url': ln.payment_proof.url if ln.payment_proof else '',
                'has_payment_proof': bool(ln.payment_proof),
                'customer_self_charge_allowed': ln.customer_self_charge_allowed,
                'customer_message': _compliance_customer_message(ln.fee_line),
                'payment_instructions': compliance_payment_instructions_serialized(ln.fee_line),
            }
        )
    transfer_tx = session.transfer_transaction
    return {
        'session_id': str(session.id),
        'flow': session.flow,
        'status': session.status,
        'compliance_scope': session.compliance_scope,
        'principal_amount': str(session.principal_amount),
        'expires_at': session.expires_at.isoformat(),
        'transfer_transaction_id': str(transfer_tx.id) if transfer_tx else None,
        'transfer_reference': transfer_tx.reference_number if transfer_tx else None,
        'transfer_status': transfer_tx.status if transfer_tx else None,
        'lines': lines,
    }


def _session_destination_number(session: RegulatedTransferSession) -> str | None:
    if session.to_account_id and session.to_account:
        return session.to_account.account_number
    tx = session.transfer_transaction
    if tx and tx.metadata:
        return tx.metadata.get('destination_account_number')
    return None


def assert_session_ready_for_international_transfer(
    session_id,
    user,
    from_account_id,
    destination_account_number: str,
    amount,
    international_wire: dict | None = None,
) -> RegulatedTransferSession:
    from .services import normalize_destination_account_number

    dest_number = normalize_destination_account_number(destination_account_number)
    session = RegulatedTransferSession.objects.select_related(
        'from_account',
        'to_account',
        'transfer_transaction',
    ).get(id=session_id)
    if session.user_id != user.id:
        raise RegulatedFlowError('Access denied.')
    if session.flow != RegulatedTransferSession.Flow.INTERNATIONAL_TRANSFER:
        raise RegulatedFlowError('Invalid session type.')
    if str(session.from_account_id) != str(from_account_id):
        raise RegulatedFlowError('Transfer details do not match this verification session.')
    stored_dest = _session_destination_number(session)
    if stored_dest and stored_dest != dest_number:
        raise RegulatedFlowError('Transfer details do not match this verification session.')
    if Decimal(str(amount)) != session.principal_amount:
        raise RegulatedFlowError('Amount does not match this verification session.')
    if session.status != RegulatedTransferSession.Status.LINES_VERIFIED:
        raise RegulatedFlowError('Complete all fee verification steps first.')
    if timezone.now() > session.expires_at:
        raise RegulatedFlowError('Session expired.')
    stored = session.international_wire_details
    if stored is not None and international_wire != stored:
        raise RegulatedFlowError(
            'International beneficiary details do not match this verification session. Go back to step 1 and retry.',
        )
    return session


def assert_session_ready_for_loan_disburse(session_id, user, loan_application_id) -> RegulatedTransferSession:
    session = RegulatedTransferSession.objects.select_related('loan_application').get(id=session_id)
    if session.user_id != user.id:
        raise RegulatedFlowError('Access denied.')
    if session.flow != RegulatedTransferSession.Flow.LOAN_PAYOUT:
        raise RegulatedFlowError('Invalid session type.')
    if str(session.loan_application_id) != str(loan_application_id):
        raise RegulatedFlowError('Loan application does not match this session.')
    if session.status != RegulatedTransferSession.Status.LINES_VERIFIED:
        raise RegulatedFlowError('Complete all fee verification steps first.')
    if timezone.now() > session.expires_at:
        raise RegulatedFlowError('Session expired.')
    return session


@db_transaction.atomic
def mark_international_session_completed(session_id):
    RegulatedTransferSession.objects.filter(id=session_id).update(
        status=RegulatedTransferSession.Status.COMPLETED,
        updated_at=timezone.now(),
    )


@db_transaction.atomic
def mark_loan_session_completed(session_id):
    RegulatedTransferSession.objects.filter(id=session_id).update(
        status=RegulatedTransferSession.Status.COMPLETED,
        updated_at=timezone.now(),
    )
