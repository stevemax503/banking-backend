"""Outgoing From address routing for SafaPay email aliases."""
from types import SimpleNamespace

from django.test import TestCase, override_settings

from apps.notifications.email_layout import (
    get_from_email_for_event,
    render_event_email,
    resolve_contact_key,
    resolve_sender_key,
)


@override_settings(
    INFO_EMAIL='info@safapaygroup.com',
    SUPPORT_EMAIL='support@safapaygroup.com',
    SECURITY_EMAIL='security@safapaygroup.com',
    ADMIN_EMAIL='admin@safapaygroup.com',
)
class EmailSenderRoutingTests(TestCase):
    def test_welcome_from_info(self):
        self.assertEqual(
            get_from_email_for_event('registration', {}),
            'SafaPay Bank <info@safapaygroup.com>',
        )

    def test_otp_from_security(self):
        for event in ('mfa_otp', 'compliance_fee_otp', 'password_reset'):
            self.assertEqual(
                get_from_email_for_event(event, {}),
                'SafaPay Security <security@safapaygroup.com>',
            )

    def test_deposit_credit_from_info(self):
        self.assertEqual(
            get_from_email_for_event('transaction', {'direction': 'credit'}),
            'SafaPay Bank <info@safapaygroup.com>',
        )

    def test_debit_from_security(self):
        self.assertEqual(
            get_from_email_for_event('transaction', {'direction': 'debit'}),
            'SafaPay Security <security@safapaygroup.com>',
        )

    def test_support_ticket_from_support(self):
        self.assertEqual(
            get_from_email_for_event('support_update', {}),
            'SafaPay Support <support@safapaygroup.com>',
        )

    def test_profile_approval_from_admin(self):
        self.assertEqual(
            get_from_email_for_event('profile_update_approved', {}),
            'SafaPay Bank <admin@safapaygroup.com>',
        )

    def test_welcome_footer_contact_support(self):
        ctx = {
            'full_name': 'Alex',
            'user_email': 'alex@example.com',
            'user': SimpleNamespace(full_name='Alex', email='alex@example.com'),
        }
        _, text_body, _ = render_event_email('registration', ctx)
        self.assertIn('support@safapaygroup.com', text_body)

    def test_otp_footer_contact_security(self):
        _, text_body, _ = render_event_email(
            'mfa_otp',
            {'otp': '123456', 'full_name': 'Alex'},
        )
        self.assertIn('security@safapaygroup.com', text_body)

    def test_resolve_keys(self):
        self.assertEqual(resolve_sender_key('loan_approved', {}), 'info')
        self.assertEqual(resolve_contact_key('registration', {}), 'support')
        self.assertEqual(resolve_contact_key('transaction', {'direction': 'credit'}), 'support')
