"""Admin transaction list filters and edit/delete."""
import pytest
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Account, Currency
from apps.transactions.models import Transaction, TransactionFee
from apps.transactions.services import _record_fee
from apps.users.models import CustomUser

User = get_user_model()


@pytest.fixture
def currency(db):
    return Currency.objects.create(code='USD', name='US Dollar', symbol='$')


@pytest.fixture
def customer(db):
    return User.objects.create_user(email='txfilter@example.com', full_name='Filter User', password='TestPass123!')


@pytest.fixture
def staff(db):
    return CustomUser.objects.create_user(
        email='staff-tx@example.com',
        full_name='Staff Tx',
        password='TestPass123!',
        role=CustomUser.Role.SUPER_ADMIN,
    )


@pytest.fixture
def account(db, customer, currency):
    return Account.objects.create(
        owner=customer,
        currency=currency,
        account_type=Account.AccountType.CHECKING,
        balance=Decimal('500.00'),
        available_balance=Decimal('500.00'),
        account_number='4444444444444444',
    )


@pytest.mark.django_db
class TestAdminTransactions:
    def test_filter_by_user_email(self, staff, customer, account):
        Transaction.objects.create(
            transaction_type=Transaction.TransactionType.DEPOSIT,
            amount=Decimal('10'),
            currency='USD',
            to_account=account,
            status=Transaction.Status.PENDING,
            description='Test pending',
            initiated_by=customer,
        )
        client = APIClient()
        client.force_authenticate(user=staff)
        url = reverse('admin-transaction-list')
        res = client.get(url, {'user': 'txfilter@example.com'})
        assert res.status_code == 200
        results = res.data.get('results', res.data)
        assert len(results) >= 1

    def test_admin_update_status(self, staff, customer, account):
        tx = Transaction.objects.create(
            transaction_type=Transaction.TransactionType.DEPOSIT,
            amount=Decimal('25'),
            currency='USD',
            to_account=account,
            status=Transaction.Status.PENDING,
            description='Pending dep',
            fee_amount=Decimal('0'),
            initiated_by=staff,
        )
        client = APIClient()
        client.force_authenticate(user=staff)
        url = reverse('admin-transaction-update', kwargs={'pk': tx.id})
        res = client.patch(url, {'status': 'FAILED', 'amount': '25.00'}, format='json')
        assert res.status_code == 200, res.data
        tx.refresh_from_db()
        assert tx.status == Transaction.Status.FAILED

    def test_admin_delete_transaction(self, staff, customer, account):
        tx = Transaction.objects.create(
            transaction_type=Transaction.TransactionType.DEPOSIT,
            amount=Decimal('5'),
            currency='USD',
            to_account=account,
            status=Transaction.Status.PENDING,
            description='To delete',
            initiated_by=staff,
        )
        client = APIClient()
        client.force_authenticate(user=staff)
        url = reverse('admin-transaction-delete', kwargs={'pk': tx.id})
        res = client.delete(url)
        assert res.status_code == 200
        assert not Transaction.objects.filter(id=tx.id).exists()

    def test_admin_list_paginated(self, staff, customer, account):
        for i in range(3):
            Transaction.objects.create(
                transaction_type=Transaction.TransactionType.DEPOSIT,
                amount=Decimal(f'{i + 1}.00'),
                currency='USD',
                to_account=account,
                status=Transaction.Status.PENDING,
                description=f'Row {i}',
                initiated_by=customer,
            )
        client = APIClient()
        client.force_authenticate(user=staff)
        res = client.get(reverse('admin-transaction-list'), {'page_size': '2'})
        assert res.status_code == 200
        assert res.data['count'] >= 3
        assert len(res.data['results']) == 2

    def test_admin_update_syncs_fee_child_amount_and_date(self, staff, customer, account):
        TransactionFee.objects.create(
            fee_type=TransactionFee.FeeType.TRANSFER_LOCAL,
            flat_amount=Decimal('2.00'),
            percentage=Decimal('0'),
        )
        account.balance = Decimal('1000.00')
        account.available_balance = Decimal('1000.00')
        account.save(update_fields=['balance', 'available_balance'])

        parent = Transaction.objects.create(
            transaction_type=Transaction.TransactionType.TRANSFER_EXTERNAL,
            amount=Decimal('100.00'),
            currency='USD',
            from_account=account,
            status=Transaction.Status.COMPLETED,
            description='Transfer to BobTXN0000000001',
            fee_amount=Decimal('2.00'),
            initiated_by=staff,
            completed_at=timezone.now(),
        )
        _record_fee(account, Decimal('2.00'), parent, staff)
        fee_tx = Transaction.objects.get(metadata__parent_transaction_id=str(parent.id))

        new_when = timezone.datetime(2025, 3, 15, 14, 30, tzinfo=timezone.utc)
        client = APIClient()
        client.force_authenticate(user=staff)
        url = reverse('admin-transaction-update', kwargs={'pk': parent.id})
        res = client.patch(
            url,
            {
                'amount': '200.00',
                'transaction_at': new_when.isoformat().replace('+00:00', 'Z'),
            },
            format='json',
        )
        assert res.status_code == 200, res.data
        parent.refresh_from_db()
        fee_tx.refresh_from_db()
        assert parent.fee_amount == Decimal('2.00')
        assert parent.amount == Decimal('200.00')
        assert fee_tx.amount == Decimal('2.00')
        assert parent.created_at == new_when
        assert fee_tx.created_at == new_when
        assert fee_tx.completed_at == new_when

    def test_admin_delete_with_reversal_child(self, staff, customer, account):
        original = Transaction.objects.create(
            transaction_type=Transaction.TransactionType.DEPOSIT,
            amount=Decimal('40'),
            currency='USD',
            to_account=account,
            status=Transaction.Status.REVERSED,
            description='Original',
            initiated_by=staff,
        )
        reversal = Transaction.objects.create(
            transaction_type=Transaction.TransactionType.REVERSAL,
            amount=Decimal('40'),
            currency='USD',
            from_account=account,
            status=Transaction.Status.COMPLETED,
            description=f'Reversal of {original.reference_number}',
            initiated_by=staff,
            original_transaction=original,
            completed_at=timezone.now(),
        )
        client = APIClient()
        client.force_authenticate(user=staff)
        url = reverse('admin-transaction-delete', kwargs={'pk': original.id})
        res = client.delete(url)
        assert res.status_code == 200
        assert not Transaction.objects.filter(id__in=[original.id, reversal.id]).exists()
