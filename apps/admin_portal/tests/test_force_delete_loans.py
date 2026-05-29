"""Force-delete user with loans, pending applications, and transactions."""
import pytest
from decimal import Decimal

from apps.accounts.models import Account, Currency
from apps.loans.models import LoanApplication, LoanProduct
from apps.loans.services import disburse_loan
from apps.transactions.models import Transaction
from apps.transactions.regulated_flow import start_loan_payout_session
from apps.transactions.regulated_models import ComplianceFeeLine, RegulatedTransferSession
from apps.transactions.services import transfer
from apps.users.force_delete import force_delete_user
from apps.users.models import CustomUser

User = CustomUser


@pytest.fixture
def super_admin(db):
    return User.objects.create_user(
        email='loan-del-admin@example.com',
        full_name='Loan Delete Admin',
        password='TestPass123!',
        role=CustomUser.Role.SUPER_ADMIN,
    )


@pytest.fixture
def heavy_customer(db, super_admin):
    user = User.objects.create_user(
        email='heavy-customer@example.com',
        full_name='Heavy Customer',
        password='TestPass123!',
        role=CustomUser.Role.CUSTOMER,
    )
    usd, _ = Currency.objects.get_or_create(code='USD', defaults={'name': 'US Dollar', 'symbol': '$'})
    checking = Account.objects.create(
        owner=user,
        currency=usd,
        account_type=Account.AccountType.CHECKING,
        is_primary=True,
        balance=Decimal('5000'),
        available_balance=Decimal('5000'),
    )
    savings = Account.objects.create(
        owner=user,
        currency=usd,
        account_type=Account.AccountType.SAVINGS,
        balance=Decimal('1000'),
        available_balance=Decimal('1000'),
    )
    product = LoanProduct.objects.create(
        name='Personal',
        loan_type=LoanProduct.LoanType.PERSONAL,
        interest_rate=Decimal('0.1200'),
        min_amount=Decimal('1000'),
        max_amount=Decimal('50000'),
        max_term_months=60,
    )
    pending_app = LoanApplication.objects.create(
        applicant=user,
        product=product,
        requested_amount=Decimal('8000'),
        term_months=24,
        status=LoanApplication.Status.SUBMITTED,
    )
    approved_app = LoanApplication.objects.create(
        applicant=user,
        product=product,
        requested_amount=Decimal('10000'),
        term_months=36,
        status=LoanApplication.Status.APPROVED,
    )
    disburse_loan(str(approved_app.id), str(checking.id), super_admin, enforce_applicant_account=True)

    transfer(
        from_account_id=str(checking.id),
        to_account_id=str(savings.id),
        amount=Decimal('250'),
        description='Internal move',
        initiated_by=user,
    )
    Transaction.objects.filter(initiated_by=user, status=Transaction.Status.PENDING).update(
        status=Transaction.Status.COMPLETED,
    )
    return user


@pytest.mark.django_db
def test_force_delete_user_with_loans_and_transactions(super_admin, heavy_customer):
    user = heavy_customer
    assert LoanApplication.objects.filter(applicant=user).count() >= 2
    assert Transaction.objects.filter(initiated_by=user).exists()

    summary = force_delete_user(user, actor=super_admin)

    assert summary['email'] == 'heavy-customer@example.com'
    assert not User.objects.filter(pk=user.pk).exists()
    assert not LoanApplication.objects.filter(applicant_id=user.id).exists()
    assert not Transaction.objects.filter(initiated_by_id=user.id).exists()
    assert not Account.objects.filter(owner_id=user.id).exists()


@pytest.mark.django_db
def test_force_delete_user_with_active_loan_payout_session(super_admin, heavy_customer):
    user = heavy_customer
    checking = Account.objects.get(owner=user, account_type=Account.AccountType.CHECKING)
    product = LoanProduct.objects.get(name='Personal')
    payout_app = LoanApplication.objects.create(
        applicant=user,
        product=product,
        requested_amount=Decimal('7000'),
        term_months=18,
        status=LoanApplication.Status.APPROVED,
    )

    ComplianceFeeLine.objects.create(
        name='Settlement fee',
        code='loan_settle_del',
        applies_to=ComplianceFeeLine.AppliesTo.LOAN_PAYOUT,
        flat_amount=Decimal('50.00'),
    )
    session = start_loan_payout_session(user, checking, payout_app)
    assert RegulatedTransferSession.objects.filter(user=user, loan_application=payout_app).exists()

    summary = force_delete_user(user, actor=super_admin)

    assert summary['email'] == 'heavy-customer@example.com'
    assert not User.objects.filter(pk=user.pk).exists()
    assert not RegulatedTransferSession.objects.filter(id=session.id).exists()
