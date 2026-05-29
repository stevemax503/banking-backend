"""Cascade-delete a user and all banking data linked to them (admin force delete)."""

from __future__ import annotations

from django.db import transaction
from django.db.models import Q

from apps.accounts.models import Account
from apps.loans.models import LoanAccount, LoanApplication, RepaymentSchedule
from apps.support.models import SupportTicket, TicketMessage
from apps.transactions.admin_transaction import admin_delete_transactions
from apps.transactions.models import Transaction
from apps.transactions.regulated_models import RegulatedTransferSession, RegulatedTransferSessionLine
from apps.users.models import CustomUser, StaffCustomerAssignment


def _collect_regulated_transaction_ids(session_ids: list) -> list[str]:
    if not session_ids:
        return []
    tx_ids: list[str] = []
    for fee_tx_id in RegulatedTransferSessionLine.objects.filter(
        session_id__in=session_ids,
    ).values_list('fee_transaction_id', flat=True):
        if fee_tx_id:
            tx_ids.append(str(fee_tx_id))
    for transfer_tx_id in RegulatedTransferSession.objects.filter(
        id__in=session_ids,
    ).values_list('transfer_transaction_id', flat=True):
        if transfer_tx_id:
            tx_ids.append(str(transfer_tx_id))
    return list(dict.fromkeys(tx_ids))


def _detach_regulated_session_transactions(session_ids: list) -> None:
    """Clear PROTECT FKs so compliance / transfer transactions can be removed."""
    if not session_ids:
        return
    RegulatedTransferSessionLine.objects.filter(session_id__in=session_ids).update(fee_transaction=None)
    RegulatedTransferSession.objects.filter(id__in=session_ids).update(transfer_transaction=None)


def _delete_regulated_sessions(session_qs, *, actor) -> None:
    session_ids = list(session_qs.values_list('id', flat=True))
    if not session_ids:
        return

    tx_ids = _collect_regulated_transaction_ids(session_ids)
    _detach_regulated_session_transactions(session_ids)
    session_qs.delete()

    if tx_ids:
        admin_delete_transactions(tx_ids, actor=actor)


def _regulated_session_query(user_id, account_ids: list):
    query = Q(user_id=user_id) | Q(loan_application__applicant_id=user_id)
    if account_ids:
        query |= Q(from_account_id__in=account_ids) | Q(to_account_id__in=account_ids)
    return RegulatedTransferSession.objects.filter(query)


def _transaction_ids_for_user(user_id, account_ids: list) -> list[str]:
    tx_filter = Q(initiated_by_id=user_id) | Q(reversed_by_id=user_id)
    if account_ids:
        tx_filter |= Q(from_account_id__in=account_ids) | Q(to_account_id__in=account_ids)
    return [str(pk) for pk in Transaction.objects.filter(tx_filter).values_list('id', flat=True)]


def _delete_support_for_user(user_id) -> None:
    customer_ticket_ids = list(
        SupportTicket.objects.filter(customer_id=user_id).values_list('id', flat=True),
    )
    if customer_ticket_ids:
        TicketMessage.objects.filter(ticket_id__in=customer_ticket_ids).delete()
        SupportTicket.objects.filter(id__in=customer_ticket_ids).delete()
    TicketMessage.objects.filter(author_id=user_id).delete()


def _delete_loan_application(application: LoanApplication, *, actor) -> None:
    _delete_regulated_sessions(
        RegulatedTransferSession.objects.filter(loan_application_id=application.id),
        actor=actor,
    )
    try:
        loan_account = application.loan_account
    except LoanAccount.DoesNotExist:
        loan_account = None
    if loan_account:
        RepaymentSchedule.objects.filter(loan_account=loan_account).delete()
        loan_account.delete()
    application.delete()


@transaction.atomic
def force_delete_user(user: CustomUser, *, actor) -> dict:
    """
    Permanently remove user and linked portfolio (accounts, transactions, loans, tickets, etc.).
    Returns counts for audit logging.
    """
    user_id = user.id
    account_ids = list(Account.objects.filter(owner_id=user_id).values_list('id', flat=True))

    _delete_regulated_sessions(_regulated_session_query(user_id, account_ids), actor=actor)
    _delete_support_for_user(user_id)

    tx_ids = _transaction_ids_for_user(user_id, account_ids)
    deleted_tx_count = admin_delete_transactions(tx_ids, actor=actor) if tx_ids else 0

    for application in list(LoanApplication.objects.filter(applicant_id=user_id)):
        _delete_loan_application(application, actor=actor)

    StaffCustomerAssignment.objects.filter(
        Q(staff_id=user_id) | Q(customer_id=user_id) | Q(assigned_by_id=user_id),
    ).delete()

    deleted_accounts, _ = Account.objects.filter(owner_id=user_id).delete()

    email = user.email
    user.delete()

    return {
        'email': email,
        'deleted_transactions': deleted_tx_count,
        'deleted_accounts': deleted_accounts,
    }
