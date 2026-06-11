"""Per-user compliance fee lines override global lines."""
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from apps.accounts.models import Account, Currency
from apps.transactions.regulated_flow import applicable_compliance_lines
from apps.transactions.regulated_models import ComplianceFeeLine, RegulatedTransferSession

User = get_user_model()


@pytest.fixture
def currency(db):
    return Currency.objects.create(code='USD', name='US Dollar', symbol='$')


@pytest.fixture
def user(db):
    return User.objects.create_user(email='override@example.com', full_name='Override User', password='TestPass123!')


@pytest.fixture
def other_user(db):
    return User.objects.create_user(email='other@example.com', full_name='Other User', password='TestPass123!')


@pytest.fixture
def sender(db, user, currency):
    return Account.objects.create(
        owner=user,
        currency=currency,
        account_type=Account.AccountType.CHECKING,
        balance=Decimal('10000.00'),
        available_balance=Decimal('10000.00'),
        account_number='1111111111111111',
    )


@pytest.fixture
def recipient(db, other_user, currency):
    return Account.objects.create(
        owner=other_user,
        currency=currency,
        account_type=Account.AccountType.CHECKING,
        balance=Decimal('0'),
        available_balance=Decimal('0'),
        account_number='2222222222222222',
    )


@pytest.fixture
def global_line(db):
    return ComplianceFeeLine.objects.create(
        name='Global AML',
        code='global-aml',
        applies_to=ComplianceFeeLine.AppliesTo.INTERNATIONAL_TRANSFER,
        flat_amount=Decimal('25.00'),
        sort_order=10,
    )


@pytest.fixture
def user_line(db, user):
    return ComplianceFeeLine.objects.create(
        name='VIP Tax',
        code='vip-tax',
        user=user,
        applies_to=ComplianceFeeLine.AppliesTo.INTERNATIONAL_TRANSFER,
        flat_amount=Decimal('10.00'),
        sort_order=5,
    )


@pytest.mark.django_db
class TestComplianceFeeUserOverride:
    def test_global_lines_when_no_user_override(self, global_line, sender, user):
        flow = RegulatedTransferSession.Flow.INTERNATIONAL_TRANSFER
        lines = applicable_compliance_lines(flow, Decimal('1000'), sender)
        assert len(lines) == 1
        assert lines[0].code == 'global-aml'

    def test_user_lines_bypass_global(self, global_line, user_line, sender, user):
        flow = RegulatedTransferSession.Flow.INTERNATIONAL_TRANSFER
        lines = applicable_compliance_lines(flow, Decimal('1000'), sender)
        assert len(lines) == 1
        assert lines[0].code == 'vip-tax'

    def test_other_user_still_uses_global(self, global_line, user_line, recipient, other_user):
        flow = RegulatedTransferSession.Flow.INTERNATIONAL_TRANSFER
        lines = applicable_compliance_lines(flow, Decimal('1000'), recipient)
        assert len(lines) == 1
        assert lines[0].code == 'global-aml'

    def test_inactive_user_line_does_not_trigger_override(self, global_line, sender, user):
        ComplianceFeeLine.objects.create(
            name='Inactive custom',
            code='inactive-custom',
            user=user,
            applies_to=ComplianceFeeLine.AppliesTo.INTERNATIONAL_TRANSFER,
            flat_amount=Decimal('5.00'),
            is_active=False,
        )
        flow = RegulatedTransferSession.Flow.INTERNATIONAL_TRANSFER
        lines = applicable_compliance_lines(flow, Decimal('1000'), sender)
        assert len(lines) == 1
        assert lines[0].code == 'global-aml'

    def test_loan_payout_falls_back_to_global_when_user_only_has_intl_lines(
        self, global_line, user_line, sender, user,
    ):
        global_loan = ComplianceFeeLine.objects.create(
            name='Global loan fee',
            code='global-loan',
            applies_to=ComplianceFeeLine.AppliesTo.LOAN_PAYOUT,
            flat_amount=Decimal('50.00'),
        )
        flow = RegulatedTransferSession.Flow.LOAN_PAYOUT
        lines = applicable_compliance_lines(flow, Decimal('10000'), sender)
        assert len(lines) == 1
        assert lines[0].code == 'global-loan'

    def test_loan_payout_uses_user_line_when_configured(self, global_line, sender, user):
        ComplianceFeeLine.objects.create(
            name='Custom loan fee',
            code='custom-loan',
            user=user,
            applies_to=ComplianceFeeLine.AppliesTo.LOAN_PAYOUT,
            flat_amount=Decimal('75.00'),
        )
        ComplianceFeeLine.objects.create(
            name='Global loan fee',
            code='global-loan-2',
            applies_to=ComplianceFeeLine.AppliesTo.LOAN_PAYOUT,
            flat_amount=Decimal('50.00'),
        )
        flow = RegulatedTransferSession.Flow.LOAN_PAYOUT
        lines = applicable_compliance_lines(flow, Decimal('10000'), sender)
        assert len(lines) == 1
        assert lines[0].code == 'custom-loan'


@pytest.mark.unit
class TestComplianceFeesExempt:
    def test_exempt_customer_gets_no_lines(self, user, sender, db):
        ComplianceFeeLine.objects.create(
            name='Global intl',
            code='global-intl-exempt',
            applies_to=ComplianceFeeLine.AppliesTo.INTERNATIONAL_TRANSFER,
            flat_amount=Decimal('25.00'),
        )
        ComplianceFeeLine.objects.create(
            user=user,
            name='Personal intl',
            code='personal-intl-exempt',
            applies_to=ComplianceFeeLine.AppliesTo.INTERNATIONAL_TRANSFER,
            flat_amount=Decimal('99.00'),
        )
        flow = RegulatedTransferSession.Flow.INTERNATIONAL_TRANSFER
        assert len(applicable_compliance_lines(flow, Decimal('1000'), sender)) >= 1

        user.compliance_fees_exempt = True
        user.save(update_fields=['compliance_fees_exempt'])
        assert applicable_compliance_lines(flow, Decimal('1000'), sender) == []

    def test_exempt_skips_loan_payout_compliance(self, user, sender, db):
        from apps.transactions.regulated_flow import loan_payout_requires_regulated_session

        ComplianceFeeLine.objects.create(
            name='Loan fee',
            code='loan-exempt',
            applies_to=ComplianceFeeLine.AppliesTo.LOAN_PAYOUT,
            flat_amount=Decimal('40.00'),
        )
        assert loan_payout_requires_regulated_session(Decimal('5000'), sender) is True
        user.compliance_fees_exempt = True
        user.save(update_fields=['compliance_fees_exempt'])
        assert loan_payout_requires_regulated_session(Decimal('5000'), sender) is False

    def test_exempt_international_transfer_still_requires_transfer_otp(self, user, sender, db):
        from apps.transactions.services import preview_transfer_fees_for_account_number

        ComplianceFeeLine.objects.create(
            name='Global intl',
            code='global-intl-otp',
            applies_to=ComplianceFeeLine.AppliesTo.INTERNATIONAL_TRANSFER,
            flat_amount=Decimal('25.00'),
        )
        user.compliance_fees_exempt = True
        user.save(update_fields=['compliance_fees_exempt'])

        preview = preview_transfer_fees_for_account_number(
            str(sender.id),
            '2536627728123456',
            Decimal('1000.00'),
            'TRANSFER_INTERNATIONAL',
        )
        assert preview['requires_otp'] is True
        assert preview['requires_regulated_session'] is False
        assert preview.get('compliance_fee_total', '0') in ('0', '0.00', 0, Decimal('0'))
