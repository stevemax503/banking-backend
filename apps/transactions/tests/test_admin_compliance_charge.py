"""Staff and customer compliance fee external payment flow."""
import pytest
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Account, Currency
from apps.transactions.regulated_models import (
    ComplianceFeeLine,
    RegulatedTransferSession,
    RegulatedTransferSessionLine,
)
from apps.users.models import CustomUser
from apps.transactions.regulated_flow import (
    PURPOSE_REGULATED_FEE,
    RegulatedFlowError,
    start_international_session,
    verify_line_otp,
)
from apps.users.email_otp import create_email_otp
from apps.transactions.tests.test_intl_wire import _full_raw
from apps.transactions.intl_wire import validate_and_normalize_international_details

User = get_user_model()

PAYMENT_LINE_KWARGS = {
    'payment_wire_enabled': True,
    'wire_beneficiary_name': 'Compliance Desk',
    'wire_bank_name': 'Example Bank',
    'wire_swift_bic': 'EXAMUS33',
    'wire_iban': 'GB00EXAM123456',
}


@pytest.fixture
def currency(db):
    return Currency.objects.create(code='USD', name='US Dollar', symbol='$')


@pytest.fixture
def customer(db):
    return User.objects.create_user(email='cust@example.com', full_name='Customer', password='TestPass123!')


@pytest.fixture
def staff(db):
    return CustomUser.objects.create_user(
        email='staff@example.com',
        full_name='Staff',
        password='TestPass123!',
        role=CustomUser.Role.SUPER_ADMIN,
    )


@pytest.fixture
def sender(db, customer, currency):
    return Account.objects.create(
        owner=customer,
        currency=currency,
        account_type=Account.AccountType.CHECKING,
        balance=Decimal('50000.00'),
        available_balance=Decimal('50000.00'),
        account_number='1111111111111111',
    )


@pytest.fixture
def compliance_line(db):
    return ComplianceFeeLine.objects.create(
        name='AML',
        code='aml',
        applies_to=ComplianceFeeLine.AppliesTo.INTERNATIONAL_TRANSFER,
        flat_amount=Decimal('25.00'),
        **PAYMENT_LINE_KWARGS,
    )


@pytest.fixture
def wire():
    return validate_and_normalize_international_details(_full_raw())


@pytest.mark.django_db
class TestAdminComplianceExternalPayment:
    def test_customer_submit_payment_blocked_when_not_allowed(self, customer, sender, compliance_line, wire):
        session = start_international_session(
            customer,
            sender,
            '2222222222222222',
            Decimal('1000'),
            'TRANSFER_INTERNATIONAL',
            international_wire_details=wire,
        )
        line = session.lines.first()
        client = APIClient()
        client.force_authenticate(user=customer)
        url = reverse(
            'regulated-line-submit-payment',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        res = client.post(url)
        assert res.status_code == 400
        assert 'authorize' in res.data['detail'].lower() or 'wait' in res.data['detail'].lower()

    def test_customer_submit_shows_admin_message_when_not_allowed(
        self, customer, staff, sender, compliance_line, wire,
    ):
        compliance_line.customer_message = 'Please contact support to continue.'
        compliance_line.save(update_fields=['customer_message', 'updated_at'])
        session = start_international_session(
            customer,
            sender,
            '2222222222222222',
            Decimal('1000'),
            'TRANSFER_INTERNATIONAL',
            international_wire_details=wire,
        )
        line = session.lines.first()
        client = APIClient()
        client.force_authenticate(user=customer)
        url = reverse(
            'regulated-line-submit-payment',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        res = client.post(url)
        assert res.status_code == 400, res.data
        assert res.data['detail'] == 'Please contact support to continue.'

    def test_allow_requires_payment_configuration(self, customer, staff, sender, wire):
        bare_line = ComplianceFeeLine.objects.create(
            name='Unconfigured',
            code='bare',
            applies_to=ComplianceFeeLine.AppliesTo.INTERNATIONAL_TRANSFER,
            flat_amount=Decimal('10.00'),
        )
        session = start_international_session(
            customer,
            sender,
            '2222222222222222',
            Decimal('1000'),
            'TRANSFER_INTERNATIONAL',
            international_wire_details=wire,
        )
        line = session.lines.filter(fee_line=bare_line).first()
        admin = APIClient()
        admin.force_authenticate(user=staff)
        allow_url = reverse(
            'admin-regulated-line-allow-customer-charge',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        res = admin.post(allow_url)
        assert res.status_code == 400
        assert 'configure' in res.data['detail'].lower()

    def test_allow_then_customer_submits_payment(self, customer, staff, sender, compliance_line, wire):
        session = start_international_session(
            customer,
            sender,
            '2222222222222222',
            Decimal('1000'),
            'TRANSFER_INTERNATIONAL',
            international_wire_details=wire,
        )
        line = session.lines.first()
        admin = APIClient()
        admin.force_authenticate(user=staff)
        allow_url = reverse(
            'admin-regulated-line-allow-customer-charge',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        assert admin.post(allow_url).status_code == 200
        line.refresh_from_db()
        assert line.customer_self_charge_allowed is True
        assert line.payment_reference

        cust = APIClient()
        cust.force_authenticate(user=customer)
        submit_url = reverse(
            'regulated-line-submit-payment',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        res = cust.post(submit_url)
        assert res.status_code == 200, res.data
        line.refresh_from_db()
        assert line.status == RegulatedTransferSessionLine.Status.PAYMENT_SUBMITTED

    def test_custom_payment_reference_from_fee_line(self, customer, staff, sender, compliance_line, wire):
        compliance_line.custom_payment_reference = 'LP-CUSTOM-REF'
        compliance_line.save(update_fields=['custom_payment_reference'])
        session = start_international_session(
            customer,
            sender,
            '2222222222222222',
            Decimal('1000'),
            'TRANSFER_INTERNATIONAL',
            international_wire_details=wire,
        )
        line = session.lines.first()
        admin = APIClient()
        admin.force_authenticate(user=staff)
        allow_url = reverse(
            'admin-regulated-line-allow-customer-charge',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        assert admin.post(allow_url).status_code == 200
        line.refresh_from_db()
        assert line.payment_reference == 'LP-CUSTOM-REF'

    def test_auto_generates_payment_reference_when_custom_blank(
        self, customer, staff, sender, compliance_line, wire,
    ):
        session = start_international_session(
            customer,
            sender,
            '2222222222222222',
            Decimal('1000'),
            'TRANSFER_INTERNATIONAL',
            international_wire_details=wire,
        )
        line = session.lines.first()
        admin = APIClient()
        admin.force_authenticate(user=staff)
        allow_url = reverse(
            'admin-regulated-line-allow-customer-charge',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        assert admin.post(allow_url).status_code == 200
        line.refresh_from_db()
        assert line.payment_reference.startswith('CMP-')

    def test_admin_cannot_confirm_before_customer_submits(
        self, customer, staff, sender, compliance_line, wire,
    ):
        session = start_international_session(
            customer,
            sender,
            '2222222222222222',
            Decimal('1000'),
            'TRANSFER_INTERNATIONAL',
            international_wire_details=wire,
        )
        line = session.lines.first()
        admin = APIClient()
        admin.force_authenticate(user=staff)
        allow_url = reverse(
            'admin-regulated-line-allow-customer-charge',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        assert admin.post(allow_url).status_code == 200

        confirm_url = reverse(
            'admin-regulated-line-confirm-payment',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        res = admin.post(confirm_url)
        assert res.status_code == 400
        assert 'customer' in res.data['detail'].lower()

    def test_compliance_otp_single_use_and_expires_after_48_hours(
        self, customer, staff, sender, compliance_line, wire,
    ):
        from apps.users.email_otp import COMPLIANCE_OTP_VALIDITY_HOURS
        from apps.users.models import EmailOTPToken

        session = start_international_session(
            customer,
            sender,
            '2222222222222222',
            Decimal('1000'),
            'TRANSFER_INTERNATIONAL',
            international_wire_details=wire,
        )
        line = session.lines.first()
        admin = APIClient()
        admin.force_authenticate(user=staff)
        allow_url = reverse(
            'admin-regulated-line-allow-customer-charge',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        assert admin.post(allow_url).status_code == 200

        cust = APIClient()
        cust.force_authenticate(user=customer)
        submit_url = reverse(
            'regulated-line-submit-payment',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        assert cust.post(submit_url).status_code == 200

        confirm_url = reverse(
            'admin-regulated-line-confirm-payment',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        assert admin.post(confirm_url).status_code == 200

        otp_url = reverse(
            'admin-regulated-line-charge-send-otp',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        res = admin.post(otp_url)
        assert res.status_code == 200, res.data
        code = res.data['otp']

        token = EmailOTPToken.objects.get(
            user=customer,
            purpose='regulated_fee',
            context_id=line.id,
            token=code,
        )
        assert COMPLIANCE_OTP_VALIDITY_HOURS == 48

        verify_line_otp(line.id, customer, code)
        line.refresh_from_db()
        assert line.status == RegulatedTransferSessionLine.Status.OTP_VERIFIED
        token.refresh_from_db()
        assert token.is_used is True

        session.refresh_from_db()
        session.status = RegulatedTransferSession.Status.IN_PROGRESS
        session.save(update_fields=['status', 'updated_at'])
        line.status = RegulatedTransferSessionLine.Status.CHARGED
        line.save(update_fields=['status', 'updated_at'])
        with pytest.raises(RegulatedFlowError, match='Invalid|expired|used'):
            verify_line_otp(line.id, customer, code)

        expired_code = create_email_otp(customer, PURPOSE_REGULATED_FEE, line.id)
        EmailOTPToken.objects.filter(
            user=customer,
            purpose='regulated_fee',
            context_id=line.id,
            token=expired_code,
        ).update(expires_at=timezone.now() - timedelta(hours=49))
        with pytest.raises(RegulatedFlowError, match='Invalid|expired|used'):
            verify_line_otp(line.id, customer, expired_code)

    def test_admin_confirm_then_send_otp_without_balance_debit(
        self, customer, staff, sender, compliance_line, wire,
    ):
        session = start_international_session(
            customer,
            sender,
            '2222222222222222',
            Decimal('1000'),
            'TRANSFER_INTERNATIONAL',
            international_wire_details=wire,
        )
        line = session.lines.first()
        sender.available_balance = Decimal('0')
        sender.balance = Decimal('0')
        sender.save(update_fields=['available_balance', 'balance'])

        admin = APIClient()
        admin.force_authenticate(user=staff)
        allow_url = reverse(
            'admin-regulated-line-allow-customer-charge',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        assert admin.post(allow_url).status_code == 200

        cust = APIClient()
        cust.force_authenticate(user=customer)
        submit_url = reverse(
            'regulated-line-submit-payment',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        assert cust.post(submit_url).status_code == 200

        confirm_url = reverse(
            'admin-regulated-line-confirm-payment',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        assert admin.post(confirm_url).status_code == 200
        line.refresh_from_db()
        assert line.status == RegulatedTransferSessionLine.Status.PAYMENT_CONFIRMED

        otp_url = reverse(
            'admin-regulated-line-charge-send-otp',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        res = admin.post(otp_url)
        assert res.status_code == 200, res.data
        assert res.data.get('otp')
        assert len(res.data['otp']) == 6
        line.refresh_from_db()
        assert line.status == RegulatedTransferSessionLine.Status.CHARGED
        assert line.fee_transaction_id is None
        sender.refresh_from_db()
        assert sender.available_balance == Decimal('0')

    def test_customer_submit_with_optional_proof(self, customer, staff, sender, compliance_line, wire):
        from django.core.files.uploadedfile import SimpleUploadedFile

        session = start_international_session(
            customer,
            sender,
            '2222222222222222',
            Decimal('1000'),
            'TRANSFER_INTERNATIONAL',
            international_wire_details=wire,
        )
        line = session.lines.first()
        admin = APIClient()
        admin.force_authenticate(user=staff)
        allow_url = reverse(
            'admin-regulated-line-allow-customer-charge',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        assert admin.post(allow_url).status_code == 200

        cust = APIClient()
        cust.force_authenticate(user=customer)
        submit_url = reverse(
            'regulated-line-submit-payment',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        proof = SimpleUploadedFile('receipt.png', b'fake-png-bytes', content_type='image/png')
        res = cust.post(submit_url, {'payment_proof': proof}, format='multipart')
        assert res.status_code == 200, res.data
        line.refresh_from_db()
        assert line.payment_proof
        assert line.status == RegulatedTransferSessionLine.Status.PAYMENT_SUBMITTED

    def test_otp_persisted_when_email_queue_fails(
        self, customer, staff, sender, compliance_line, wire,
    ):
        session = start_international_session(
            customer,
            sender,
            '2222222222222222',
            Decimal('1000'),
            'TRANSFER_INTERNATIONAL',
            international_wire_details=wire,
        )
        line = session.lines.first()
        admin = APIClient()
        admin.force_authenticate(user=staff)
        allow_url = reverse(
            'admin-regulated-line-allow-customer-charge',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        assert admin.post(allow_url).status_code == 200

        cust = APIClient()
        cust.force_authenticate(user=customer)
        submit_url = reverse(
            'regulated-line-submit-payment',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        assert cust.post(submit_url).status_code == 200

        confirm_url = reverse(
            'admin-regulated-line-confirm-payment',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        assert admin.post(confirm_url).status_code == 200

        from apps.users.models import EmailOTPToken

        with patch('apps.transactions.regulated_flow.queue_email_notification', side_effect=RuntimeError('smtp down')):
            otp_url = reverse(
                'admin-regulated-line-charge-send-otp',
                kwargs={'session_id': session.id, 'line_id': line.id},
            )
            res = admin.post(otp_url)
        assert res.status_code == 200, res.data
        assert res.data.get('otp')
        line.refresh_from_db()
        assert line.status == RegulatedTransferSessionLine.Status.CHARGED
        assert EmailOTPToken.objects.filter(
            user=customer,
            purpose='regulated_fee',
            context_id=line.id,
            token=res.data['otp'],
        ).exists()

    def test_customer_charge_send_otp_only_resends_when_charged(
        self, customer, staff, sender, compliance_line, wire,
    ):
        session = start_international_session(
            customer,
            sender,
            '2222222222222222',
            Decimal('1000'),
            'TRANSFER_INTERNATIONAL',
            international_wire_details=wire,
        )
        line = session.lines.first()
        client = APIClient()
        client.force_authenticate(user=customer)
        url = reverse(
            'regulated-line-charge-send-otp',
            kwargs={'session_id': session.id, 'line_id': line.id},
        )
        res = client.post(url)
        assert res.status_code == 400
        assert 'confirmed' in res.data['detail'].lower()
