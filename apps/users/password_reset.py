"""Shared password-reset email issuance."""
import secrets
from datetime import timedelta

from django.utils import timezone

from apps.notifications.services import send_email_notification

from .models import PasswordResetToken


def issue_password_reset_email(user) -> str:
    token_str = secrets.token_urlsafe(48)
    PasswordResetToken.objects.create(
        user=user,
        token=token_str,
        expires_at=timezone.now() + timedelta(hours=1),
    )
    send_email_notification.delay(
        user_id=str(user.id),
        event_type='password_reset',
        context={'token': token_str, 'full_name': user.full_name},
    )
    return token_str
