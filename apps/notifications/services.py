"""
Email notification service using Django's built-in email backend.
All sending is async via Celery (or a background thread when Celery runs eager in dev).
"""
import logging
import threading

from celery import shared_task
from django.conf import settings

from .email_assets import send_branded_email
from .email_layout import EMAIL_SUBJECTS, get_from_email_for_event, render_event_email
from .in_app import IN_APP_EVENT_TYPES, build_in_app_notification, in_app_event_type_for_email

logger = logging.getLogger(__name__)

# Never stored in the in-app notification center (email only).
EMAIL_ONLY_EVENT_TYPES = frozenset({
    'mfa_otp',
    'password_reset',
    'registration',
    'low_balance',
    'loan_payment_due',
    'loan_rejected',
    'support_update',
    'profile_update_approved',
    'goal_autosave_success',
    'goal_autosave_insufficient',
    'statement_ready',
    'security_alert',
})

__all__ = [
    'EMAIL_SUBJECTS',
    'EMAIL_ONLY_EVENT_TYPES',
    'IN_APP_EVENT_TYPES',
    'send_email_notification',
    'send_transaction_notification',
    'queue_email_notification',
]


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_email_notification(self, user_id: str, event_type: str, context: dict):
    try:
        from django.contrib.auth import get_user_model
        from .models import Notification

        User = get_user_model()
        user = User.objects.get(id=user_id)
        context['user'] = user
        subject, text_body, html_body = render_event_email(event_type, context)

        send_branded_email(
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            from_email=get_from_email_for_event(event_type, context),
            recipient_list=[user.email],
            fail_silently=False,
        )

        in_app_type = in_app_event_type_for_email(event_type, context)
        highlight = build_in_app_notification(event_type, context)
        if in_app_type and highlight and in_app_type in IN_APP_EVENT_TYPES:
            bell_subject, bell_body = highlight
            notif = Notification.objects.create(
                user=user,
                event_type=in_app_type,
                subject=bell_subject,
                body=bell_body,
                email_status='SENT',
            )
            _push_notification_ws(
                str(user.id),
                {
                    'id': str(notif.id),
                    'subject': bell_subject,
                    'body': bell_body,
                    'event_type': in_app_type,
                },
            )
        elif event_type not in EMAIL_ONLY_EVENT_TYPES:
            logger.debug('No in-app notification for event %s (user %s)', event_type, user_id)
        else:
            logger.info('Email-only notification sent for %s (user %s)', event_type, user_id)
    except OSError as exc:
        logger.error('Email notification failed (network/DNS) for user %s: %s', user_id, exc, exc_info=True)
        raise
    except Exception as exc:
        logger.error(f'Email notification failed for user {user_id}: {exc}')
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def send_transaction_notification(self, transaction_id: str):
    try:
        from apps.transactions.models import Transaction
        tx = Transaction.objects.select_related(
            'from_account__owner', 'to_account__owner', 'initiated_by'
        ).get(id=transaction_id)

        notified_users = set()

        if tx.from_account and tx.from_account.owner:
            user = tx.from_account.owner
            notified_users.add(str(user.id))
            send_email_notification.delay(
                str(user.id),
                'transaction',
                {
                    'tx_type': tx.transaction_type,
                    'amount': str(tx.amount),
                    'currency': tx.currency,
                    'reference': tx.reference_number,
                    'direction': 'debit',
                    'balance': str(tx.from_account.balance),
                },
            )

        if tx.to_account and tx.to_account.owner and str(tx.to_account.owner.id) not in notified_users:
            user = tx.to_account.owner
            send_email_notification.delay(
                str(user.id),
                'transaction',
                {
                    'tx_type': tx.transaction_type,
                    'amount': str(tx.amount),
                    'currency': tx.currency,
                    'reference': tx.reference_number,
                    'direction': 'credit',
                    'balance': str(tx.to_account.balance),
                },
            )
    except Exception as exc:
        raise self.retry(exc=exc)


def queue_email_notification(user_id: str, event_type: str, context: dict) -> None:
    """
    Enqueue email without blocking the HTTP response.

    Always runs work in a daemon thread so SMTP (eager mode) or broker I/O never holds
    the request open — registration can return 201 while the welcome email sends.
    """

    def _run():
        try:
            if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
                send_email_notification.run(user_id, event_type, context)
            else:
                send_email_notification.apply_async(
                    args=[user_id, event_type, context],
                    ignore_result=True,
                )
        except Exception:
            logger.exception(
                'Background email failed for user %s (event=%s)',
                user_id,
                event_type,
            )

    threading.Thread(target=_run, daemon=True).start()


def _push_notification_ws(user_id: str, notification: dict) -> None:
    """Best-effort real-time toast + bell refresh for connected clients."""
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        async_to_sync(channel_layer.group_send)(
            f'user_{user_id}',
            {
                'type': 'notification_message',
                'message': {'type': 'notification', 'notification': notification},
            },
        )
    except Exception as exc:
        logger.debug('WS push skipped: %s', exc)
