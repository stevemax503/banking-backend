"""Admin deposits with configurable method, status, deposit source, and deposit fees."""
import pytest
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Account, Currency
from apps.transactions.models import Transaction, TransactionFee
from apps.users.models import CustomUser

User = get_user_model()

WIRE_SOURCE = {
    'originator_name': 'Acme Corp',
    'originator_bank': 'First National Bank',
    'wire_reference': 'IMAD20260517001',
}

TRANSFER_SOURCE = {
    'depositor_name': 'Jane Doe',
    'sender_bank_name': 'Chase Bank NA',
    'sender_account_number': '1234567890123456',
}


@pytest.fixture
def currency(db):
    return Currency.objects.create(code='USD', name='US Dollar', symbol='$')


@pytest.fixture
def customer(db):
    return User.objects.create_user(email='dep@example.com', full_name='Dep User', password='TestPass123!')


@pytest.fixture
def staff(db):
    return CustomUser.objects.create_user(
        email='staff-dep@example.com',
        full_name='Staff',
        password='TestPass123!',
        role=CustomUser.Role.SUPER_ADMIN,
    )


@pytest.fixture
def account(db, customer, currency):
    return Account.objects.create(
        owner=customer,
        currency=currency,
        account_type=Account.AccountType.CHECKING,
        balance=Decimal('100.00'),
        available_balance=Decimal('100.00'),
        account_number='3333333333333333',
    )


@pytest.fixture
def deposit_fee(db):
    return TransactionFee.objects.create(
        fee_type=TransactionFee.FeeType.DEPOSIT,
        flat_amount=Decimal('2.00'),
        percentage=Decimal('0'),
    )


@pytest.mark.django_db
class TestAdminDeposit:
    def test_deposit_applies_fee_and_credits_net(self, staff, account, deposit_fee):
        client = APIClient()
        client.force_authenticate(user=staff)
        url = reverse('admin-account-deposit', kwargs={'pk': account.id})
        res = client.post(
            url,
            {
                'amount': '100.00',
                'description': 'Deposit',
                'deposit_method': 'TRANSFER',
                'deposit_source': TRANSFER_SOURCE,
                'status': 'COMPLETED',
            },
            format='json',
        )
        assert res.status_code == 201, res.data
        assert res.data['fee'] == '2.00'
        assert res.data['net_credit'] == '98.00'
        account.refresh_from_db()
        assert account.balance == Decimal('198.00')
        tx = Transaction.objects.filter(transaction_type=Transaction.TransactionType.DEPOSIT).latest('created_at')
        assert tx.metadata['deposit_method'] == 'TRANSFER'
        assert 'Jane Doe' in tx.description
        assert tx.reference_number in tx.description
        assert tx.metadata.get('admin_note')
        assert 'Admin deposit' in tx.metadata['admin_note']
        assert tx.fee_amount == Decimal('2.00')

    def test_pending_deposit_does_not_credit_balance(self, staff, account, deposit_fee):
        client = APIClient()
        client.force_authenticate(user=staff)
        url = reverse('admin-account-deposit', kwargs={'pk': account.id})
        res = client.post(
            url,
            {
                'amount': '50.00',
                'description': 'Deposit',
                'deposit_method': 'WIRE',
                'deposit_source': WIRE_SOURCE,
                'status': 'PENDING',
            },
            format='json',
        )
        assert res.status_code == 201
        account.refresh_from_db()
        assert account.balance == Decimal('100.00')
        assert res.data['net_credit'] == '0'

    def test_failed_deposit_creates_reversal_mirror_lines(self, staff, account, deposit_fee):
        client = APIClient()
        client.force_authenticate(user=staff)
        url = reverse('admin-account-deposit', kwargs={'pk': account.id})
        balance_before = account.balance
        res = client.post(
            url,
            {
                'amount': '75.00',
                'description': 'Deposit',
                'deposit_method': 'WIRE',
                'deposit_source': WIRE_SOURCE,
                'status': 'FAILED',
            },
            format='json',
        )
        assert res.status_code == 201, res.data
        account.refresh_from_db()
        assert account.balance == balance_before
        related = res.data.get('related_transactions') or []
        assert len(related) == 3
        kinds = {r['transaction_type'] for r in related}
        assert kinds == {'REVERSAL', 'FEE', 'REVERSAL'}
        deposit = Transaction.objects.get(id=res.data['transaction']['id'])
        assert deposit.status == Transaction.Status.FAILED
        assert Transaction.objects.filter(
            transaction_type=Transaction.TransactionType.REVERSAL,
            metadata__mirror_kind='principal_reversal',
        ).exists()

    def test_deposit_allows_optional_source_fields(self, staff, account, deposit_fee):
        client = APIClient()
        client.force_authenticate(user=staff)
        url = reverse('admin-account-deposit', kwargs={'pk': account.id})
        res = client.post(
            url,
            {
                'amount': '10.00',
                'deposit_method': 'WIRE',
                'deposit_source': {'originator_name': 'Only Name'},
                'status': 'COMPLETED',
            },
            format='json',
        )
        assert res.status_code == 201, res.data
        tx = Transaction.objects.get(id=res.data['transaction']['id'])
        assert 'Only Name' in tx.description
        assert tx.reference_number in tx.description

    def test_deposit_fee_shares_assigned_transaction_at(self, staff, account, deposit_fee):
        client = APIClient()
        client.force_authenticate(user=staff)
        booked = timezone.datetime(2026, 6, 1, 17, 0, tzinfo=timezone.utc)
        url = reverse('admin-account-deposit', kwargs={'pk': account.id})
        res = client.post(
            url,
            {
                'amount': '280.00',
                'description': 'Deposit',
                'deposit_method': 'TRANSFER',
                'deposit_source': TRANSFER_SOURCE,
                'status': 'COMPLETED',
                'transaction_at': booked.isoformat().replace('+00:00', 'Z'),
            },
            format='json',
        )
        assert res.status_code == 201, res.data
        deposit = Transaction.objects.get(id=res.data['transaction']['id'])
        fee_tx = Transaction.objects.get(
            transaction_type=Transaction.TransactionType.FEE,
            metadata__parent_transaction_id=str(deposit.id),
        )
        assert deposit.created_at == booked
        assert deposit.completed_at == booked
        assert fee_tx.created_at == booked
        assert fee_tx.completed_at == booked

    def test_preview_fee(self, staff, deposit_fee):
        client = APIClient()
        client.force_authenticate(user=staff)
        url = reverse('admin-deposit-preview')
        res = client.get(url, {'amount': '200'})
        assert res.status_code == 200
        assert res.data['fee'] == '2.00'
        assert res.data['net_credit'] == '198.00'
