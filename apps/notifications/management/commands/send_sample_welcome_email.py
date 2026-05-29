"""
Send a sample welcome (registration) email with branded header/footer.

Usage:
  USE_CONSOLE_EMAIL_IN_DEV=false python manage.py send_sample_welcome_email --to you@example.com
"""
from types import SimpleNamespace

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.notifications.email_assets import send_branded_email
from apps.notifications.email_layout import get_from_email_for_event, render_event_email


class Command(BaseCommand):
    help = 'Send a sample welcome email (registration template).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--to',
            type=str,
            required=True,
            help='Recipient email address',
        )
        parser.add_argument('--name', type=str, default='', help='Customer full name (required)')
        parser.add_argument(
            '--email',
            type=str,
            default='',
            help='Sign-in email shown in the message (defaults to --to)',
        )

    def handle(self, *args, **options):
        recipient = (options['to'] or '').strip()
        if not recipient:
            raise CommandError('--to is required')

        display_name = (options['name'] or '').strip()
        if not display_name:
            raise CommandError('--name is required')

        if settings.EMAIL_BACKEND.endswith('console.EmailBackend'):
            raise CommandError(
                'EMAIL_BACKEND is console (dev default). Use USE_CONSOLE_EMAIL_IN_DEV=false for real SMTP.',
            )

        user_email = (options['email'] or recipient).strip()
        context = {
            'full_name': display_name,
            'user_email': user_email,
            'user': SimpleNamespace(full_name=display_name, email=user_email),
        }

        subject, text_body, html_body = render_event_email('registration', context)
        from_email = get_from_email_for_event('registration', context)

        send_branded_email(
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            from_email=from_email,
            recipient_list=[recipient],
            fail_silently=False,
        )

        self.stdout.write(self.style.SUCCESS(f'Sent welcome sample to {recipient}'))
        self.stdout.write(f'  Subject: {subject}')
        self.stdout.write(f'  Name: {display_name}')
        self.stdout.write(f'  Sign-in email: {user_email}')

        smtp_user = (getattr(settings, 'EMAIL_HOST_USER', '') or '').strip().lower()
        if smtp_user and recipient.lower() == smtp_user:
            self.stdout.write(
                self.style.WARNING('Same as SMTP account — check Sent / All Mail, not only Inbox.'),
            )
