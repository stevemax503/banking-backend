"""
Send a sample branded email (header + footer) to verify SMTP and templates.

Usage:
  python manage.py send_sample_branded_email --to you@example.com
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.notifications.email_assets import send_branded_email
from apps.notifications.email_layout import get_from_email, render_custom_email


class Command(BaseCommand):
    help = 'Send a sample SafaPay branded test email (header + footer wrapper).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--to',
            type=str,
            required=True,
            help='Recipient email address',
        )
        parser.add_argument(
            '--subject',
            type=str,
            default='SafaPay Bank — email test',
            help='Email subject line',
        )
        parser.add_argument(
            '--body',
            type=str,
            default='This is a test message to verify branded email delivery.',
            help='Plain-text message body (wrapped with branded header/footer)',
        )

    def handle(self, *args, **options):
        recipient = (options['to'] or '').strip()
        if not recipient:
            raise CommandError('--to is required')

        if settings.EMAIL_BACKEND.endswith('console.EmailBackend'):
            raise CommandError(
                'EMAIL_BACKEND is console (dev default). Real sends require SMTP in .env and '
                'USE_CONSOLE_EMAIL_IN_DEV=false, e.g.\n'
                '  USE_CONSOLE_EMAIL_IN_DEV=false python manage.py send_sample_branded_email --to you@example.com',
            )

        if not settings.EMAIL_HOST_USER and not getattr(settings, 'EMAIL_HOST', None):
            self.stdout.write(
                self.style.WARNING(
                    'EMAIL_HOST / EMAIL_HOST_USER may be unset — configure .env before sending.',
                )
            )

        from_email = get_from_email()
        subject = options['subject']
        text_body = options['body']

        _, wrapped_text, wrapped_html = render_custom_email(
            subject=subject,
            text_body=text_body,
        )

        send_branded_email(
            subject=subject,
            text_body=wrapped_text,
            html_body=wrapped_html,
            from_email=from_email,
            recipient_list=[recipient],
            fail_silently=False,
        )

        self.stdout.write(
            self.style.SUCCESS(
                f'Sent branded sample email to {recipient} (from {from_email}).',
            )
        )
        smtp_user = (getattr(settings, 'EMAIL_HOST_USER', '') or '').strip().lower()
        if smtp_user and recipient.lower() == smtp_user:
            self.stdout.write(
                self.style.WARNING(
                    'Recipient is the same as your Gmail SMTP account. Gmail often puts these in '
                    'Sent only, not Inbox. Search All Mail for the subject, or use --to with another address.',
                )
            )
