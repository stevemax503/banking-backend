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

    @override_settings(FRONTEND_URL='http://localhost:5173')
    def test_password_reset_email_uses_frontend_link(self):
        from types import SimpleNamespace

        token = 'abc_reset_token_xyz'
        _, text_body, html_body = render_event_email(
            'password_reset',
            {
                'token': token,
                'full_name': 'Alex',
                'user': SimpleNamespace(full_name='Alex'),
            },
        )
        expected = f'http://localhost:5173/auth/reset-password?token={token}'
        self.assertIn(expected, text_body)
        self.assertIn(expected, html_body)
        self.assertIn(f'href="{expected}"', html_body)
        self.assertNotIn(f'href="{token}"', html_body)

    @override_settings(FRONTEND_URL='https://www.safapaygroup.com')
    def test_password_reset_email_uses_production_frontend(self):
        token = 'prod-token-1'
        _, text_body, html_body = render_event_email(
            'password_reset',
            {'token': token, 'full_name': 'Alex'},
        )
        expected = f'https://www.safapaygroup.com/auth/reset-password?token={token}'
        self.assertIn(expected, text_body)
        self.assertIn(f'href="{expected}"', html_body)

    @override_settings(FRONTEND_URL='http://localhost:5173')
    def test_admin_password_set_email_includes_password(self):
        _, text_body, html_body = render_event_email(
            'admin_password_set',
            {'new_password': 'TempPass123!', 'full_name': 'Alex'},
        )
        self.assertIn('TempPass123!', text_body)
        self.assertIn('TempPass123!', html_body)
