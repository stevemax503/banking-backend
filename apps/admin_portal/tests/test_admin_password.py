"""Admin can set a user password or email a reset link."""
from unittest.mock import patch

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.users.models import CustomUser, PasswordResetToken


@pytest.fixture
def super_admin(db):
    return CustomUser.objects.create_user(
        email='super@bank.test',
        full_name='Super Admin',
        password='AdminPass123!',
        role=CustomUser.Role.SUPER_ADMIN,
        is_staff=True,
    )


@pytest.fixture
def scoped_admin(db):
    return CustomUser.objects.create_user(
        email='scoped@bank.test',
        full_name='Scoped Admin',
        password='StaffPass123!',
        role=CustomUser.Role.ADMIN,
        is_staff=True,
        admin_account_scope=CustomUser.AdminAccessScope.SELECTED,
    )


@pytest.fixture
def customer(db):
    return CustomUser.objects.create_user(
        email='customer@bank.test',
        full_name='Customer User',
        password='CustPass123!',
    )


@pytest.mark.django_db
class TestAdminSetPassword:
    def test_admin_sets_password_and_user_can_login(self, super_admin, customer):
        client = APIClient()
        client.force_authenticate(user=super_admin)
        url = reverse('admin-user-set-password', kwargs={'pk': customer.id})
        res = client.post(url, {
            'new_password': 'NewPass123!',
            'new_password_confirm': 'NewPass123!',
            'send_email': False,
        }, format='json')
        assert res.status_code == 200
        customer.refresh_from_db()
        assert customer.check_password('NewPass123!')

        login = client.post(reverse('auth-login'), {
            'email': customer.email,
            'password': 'NewPass123!',
        })
        assert login.status_code == 200

    @patch('apps.admin_portal.views.send_email_notification.delay')
    def test_admin_set_password_can_email_new_password(self, mock_delay, super_admin, customer):
        client = APIClient()
        client.force_authenticate(user=super_admin)
        url = reverse('admin-user-set-password', kwargs={'pk': customer.id})
        res = client.post(url, {
            'new_password': 'MailedPass123!',
            'new_password_confirm': 'MailedPass123!',
            'send_email': True,
        }, format='json')
        assert res.status_code == 200
        assert res.data['email_queued'] is True
        mock_delay.assert_called_once()
        args, kwargs = mock_delay.call_args
        assert args[1] == 'admin_password_set'
        assert kwargs['context']['new_password'] == 'MailedPass123!'

    def test_customer_cannot_set_password(self, customer):
        client = APIClient()
        client.force_authenticate(user=customer)
        url = reverse('admin-user-set-password', kwargs={'pk': customer.id})
        res = client.post(url, {
            'new_password': 'NewPass123!',
            'new_password_confirm': 'NewPass123!',
        }, format='json')
        assert res.status_code == 403

    def test_scoped_admin_cannot_set_out_of_scope_password(self, scoped_admin, customer):
        client = APIClient()
        client.force_authenticate(user=scoped_admin)
        url = reverse('admin-user-set-password', kwargs={'pk': customer.id})
        res = client.post(url, {
            'new_password': 'NewPass123!',
            'new_password_confirm': 'NewPass123!',
        }, format='json')
        assert res.status_code == 403


@pytest.mark.django_db
class TestAdminSendPasswordReset:
    @patch('apps.users.password_reset.send_email_notification.delay')
    def test_admin_sends_reset_link(self, mock_delay, super_admin, customer):
        client = APIClient()
        client.force_authenticate(user=super_admin)
        url = reverse('admin-user-send-password-reset', kwargs={'pk': customer.id})
        res = client.post(url)
        assert res.status_code == 200
        assert PasswordResetToken.objects.filter(user=customer, is_used=False).exists()
        mock_delay.assert_called_once()
        assert mock_delay.call_args.kwargs['event_type'] == 'password_reset'

    def test_inactive_user_cannot_receive_reset(self, super_admin, customer):
        customer.is_active = False
        customer.save(update_fields=['is_active'])
        client = APIClient()
        client.force_authenticate(user=super_admin)
        url = reverse('admin-user-send-password-reset', kwargs={'pk': customer.id})
        res = client.post(url)
        assert res.status_code == 400
