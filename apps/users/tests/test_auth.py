"""Integration tests for auth endpoints."""
import time
from unittest.mock import patch

import pytest
from django.urls import reverse
from rest_framework.test import APIClient
from django.contrib.auth import get_user_model

User = get_user_model()


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def registered_user(db):
    return User.objects.create_user(
        email='user@test.com',
        full_name='Test User',
        password='TestPass123!',
    )


@pytest.mark.integration
class TestRegistration:
    @patch('apps.users.views.queue_email_notification')
    def test_register_creates_user(self, mock_queue_email, api_client, db):
        url = reverse('auth-register')
        data = {
            'email': 'new@test.com',
            'full_name': 'New User',
            'password': 'SecurePass123!',
            'password_confirm': 'SecurePass123!',
        }
        response = api_client.post(url, data)
        assert response.status_code == 201
        assert User.objects.filter(email='new@test.com').exists()
        mock_queue_email.assert_called_once()

    @patch('apps.notifications.email_assets.send_branded_email')
    def test_register_returns_201_before_slow_smtp(self, mock_send, api_client, db):
        """Welcome email must not block the register HTTP response."""
        mock_send.side_effect = lambda **kwargs: time.sleep(3)

        url = reverse('auth-register')
        started = time.monotonic()
        response = api_client.post(url, {
            'email': 'slowemail@test.com',
            'full_name': 'Slow Email',
            'password': 'SecurePass123!',
            'password_confirm': 'SecurePass123!',
        })
        elapsed = time.monotonic() - started

        assert response.status_code == 201
        assert elapsed < 2.0
        assert User.objects.filter(email='slowemail@test.com').exists()

    def test_register_duplicate_email(self, api_client, registered_user):
        url = reverse('auth-register')
        data = {
            'email': registered_user.email,
            'full_name': 'Dup',
            'password': 'SecurePass123!',
            'password_confirm': 'SecurePass123!',
        }
        response = api_client.post(url, data)
        assert response.status_code == 400
        assert 'already exists' in str(response.data).lower()

    def test_register_password_mismatch(self, api_client, db):
        url = reverse('auth-register')
        data = {
            'email': 'mismatch@test.com',
            'full_name': 'Mismatch',
            'password': 'SecurePass123!',
            'password_confirm': 'DifferentPass456!',
        }
        response = api_client.post(url, data)
        assert response.status_code == 400


@pytest.mark.integration
class TestLogin:
    def test_login_returns_tokens(self, api_client, registered_user):
        url = reverse('auth-login')
        response = api_client.post(url, {'email': 'user@test.com', 'password': 'TestPass123!'})
        assert response.status_code == 200
        assert 'access' in response.data or 'mfa_required' in response.data

    def test_login_wrong_password(self, api_client, registered_user):
        url = reverse('auth-login')
        response = api_client.post(url, {'email': 'user@test.com', 'password': 'WrongPassword'})
        assert response.status_code == 401

    def test_locked_user_can_login_and_read_profile(self, api_client, registered_user):
        registered_user.is_locked = True
        registered_user.is_active = True
        registered_user.save(update_fields=['is_locked', 'is_active'])
        login = api_client.post(reverse('auth-login'), {
            'email': 'user@test.com',
            'password': 'TestPass123!',
        })
        assert login.status_code == 200
        api_client.credentials(HTTP_AUTHORIZATION=f'Bearer {login.data["access"]}')
        profile = api_client.get(reverse('auth-profile'))
        assert profile.status_code == 200
        assert profile.data['is_locked'] is True

    def test_locked_user_cannot_transfer(self, api_client, registered_user, db):
        from decimal import Decimal
        from apps.accounts.models import Account, Currency

        registered_user.is_locked = True
        registered_user.save(update_fields=['is_locked'])
        currency = Currency.objects.create(code='USD', name='US Dollar', symbol='$')
        Account.objects.create(
            owner=registered_user,
            currency=currency,
            account_type=Account.AccountType.SAVINGS,
            balance=Decimal('1000.00'),
            available_balance=Decimal('1000.00'),
            account_number='1111222233334444',
        )
        api_client.force_authenticate(registered_user)
        list_res = api_client.get(reverse('transaction-list'))
        assert list_res.status_code == 200
        transfer_res = api_client.post(reverse('transaction-transfer'), {
            'from_account_id': str(registered_user.accounts.first().id),
            'to_account_id': '5555666677778888',
            'amount': '10.00',
            'transfer_type': 'TRANSFER_INTERNAL',
        }, format='json')
        assert transfer_res.status_code == 403
        assert 'locked' in str(transfer_res.data).lower()
