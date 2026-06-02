"""Preview fees from UAE IBAN or 16-digit account without requiring the account in the database."""
import pytest
from decimal import Decimal

from django.contrib.auth import get_user_model

from apps.accounts.models import Account, Currency
from apps.accounts.services import build_ae_iban
from apps.transactions.models import Transaction
from apps.transactions.services import (
    preview_transfer_fees_for_account_number,
    TransactionError,
)

User = get_user_model()


@pytest.fixture
def currency(db):
    return Currency.objects.create(code='USD', name='US Dollar', symbol='$')


@pytest.fixture
def user(db):
    return User.objects.create_user(email='sender@example.com', full_name='Sender', password='TestPass123!')


@pytest.fixture
def sender(db, user, currency):
    return Account.objects.create(
        owner=user,
        currency=currency,
        account_type=Account.AccountType.CHECKING,
        balance=Decimal('5000.00'),
        available_balance=Decimal('5000.00'),
        account_number='1111111111111111',
    )


@pytest.mark.unit
class TestPreviewByAccountNumber:
    def test_unknown_account_number_still_previews_fees(self, sender):
        preview = preview_transfer_fees_for_account_number(
            str(sender.id),
            '2536627728123456',
            Decimal('1000.00'),
            Transaction.TransactionType.TRANSFER_EXTERNAL,
        )
        assert preview['amount'] == '1000.00'
        assert Decimal(preview['total_debit']) >= Decimal('1000.00')
        assert preview['destination']['last_four'] == '3456'
        assert preview['destination']['account_number'] == '2536627728123456'

    def test_rejects_wrong_digit_count(self, sender):
        with pytest.raises(TransactionError, match='11 to 16'):
            preview_transfer_fees_for_account_number(
                str(sender.id),
                '12345',
                Decimal('100.00'),
                Transaction.TransactionType.TRANSFER_INTERNAL,
            )

    def test_preview_accepts_uae_iban(self, sender):
        domestic = '2536627728123456'
        iban = build_ae_iban('033', domestic)
        preview = preview_transfer_fees_for_account_number(
            str(sender.id),
            iban,
            Decimal('100.00'),
            Transaction.TransactionType.TRANSFER_INTERNAL,
        )
        assert preview['destination']['account_number'] == domestic

    def test_rejects_same_account_number_as_sender(self, sender):
        with pytest.raises(TransactionError, match='different'):
            preview_transfer_fees_for_account_number(
                str(sender.id),
                sender.account_number,
                Decimal('100.00'),
                Transaction.TransactionType.TRANSFER_INTERNAL,
            )
