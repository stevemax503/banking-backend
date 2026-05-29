"""Force-delete user with regulated compliance sessions and transactions."""
import pytest
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from rest_framework.test import APIClient

from apps.accounts.models import Account, Currency
from apps.transactions.models import Transaction
from apps.transactions.regulated_models import (
    ComplianceFeeLine,
    RegulatedTransferSession,
    RegulatedTransferSessionLine,
)
from apps.users.force_delete import force_delete_user
from apps.users.models import CustomUser

User = get_user_model()


@pytest.fixture
def super_admin(db):
    return User.objects.create_user(
        email='force-del-admin@example.com',
        full_name='Force Delete Admin',
        password='TestPass123!',
        role=CustomUser.Role.SUPER_ADMIN,
    )


@pytest.fixture
def customer_with_compliance(db):
    user = User.objects.create_user(
        email='force-del-cust@example.com',
        full_name='Force Delete Customer',
        password='TestPass123!',
        role=CustomUser.Role.CUSTOMER,
    )
    usd, _ = Currency.objects.get_or_create(code='USD', defaults={'name': 'US Dollar', 'symbol': '$'})
    account = Account.objects.create(
        owner=user,
        currency=usd,
        account_type=Account.AccountType.CHECKING,
        is_primary=True,
        balance=Decimal('1000'),
        available_balance=Decimal('1000'),
    )
    fee_line = ComplianceFeeLine.objects.create(
        user=user,
        name='AML Check',
        code='aml-check',
        flat_amount=Decimal('25'),
    )
    tx = Transaction.objects.create(
        from_account=account,
        amount=Decimal('25'),
        currency='USD',
        transaction_type=Transaction.TransactionType.FEE,
        status=Transaction.Status.COMPLETED,
        initiated_by=user,
        description='Compliance fee',
    )
    session = RegulatedTransferSession.objects.create(
        user=user,
        flow=RegulatedTransferSession.Flow.INTERNATIONAL_TRANSFER,
        from_account=account,
        principal_amount=Decimal('500'),
        transfer_type='TRANSFER_INTERNATIONAL',
        expires_at=timezone.now() + timedelta(hours=24),
        transfer_transaction=tx,
    )
    RegulatedTransferSessionLine.objects.create(
        session=session,
        fee_line=fee_line,
        sequence=1,
        amount=Decimal('25'),
        fee_transaction=tx,
    )
    return user


@pytest.mark.django_db
def test_force_delete_clears_regulated_session_transactions(super_admin, customer_with_compliance):
    user = customer_with_compliance
    assert RegulatedTransferSession.objects.filter(user=user).exists()
    assert Transaction.objects.filter(initiated_by=user).exists()

    summary = force_delete_user(user, actor=super_admin)

    assert summary['email'] == 'force-del-cust@example.com'
    assert not User.objects.filter(pk=user.pk).exists()
    assert not RegulatedTransferSession.objects.filter(user_id=user.id).exists()
    assert not Transaction.objects.filter(initiated_by_id=user.id).exists()


@pytest.mark.django_db
def test_admin_api_delete_user_with_compliance(super_admin, customer_with_compliance):
    client = APIClient()
    client.force_authenticate(user=super_admin)
    url = reverse('admin-user-detail', kwargs={'pk': customer_with_compliance.id})
    res = client.delete(url)
    assert res.status_code == 200
    assert not User.objects.filter(pk=customer_with_compliance.id).exists()
