"""
Branded wrapper for all outgoing SafaPay Bank emails (header + footer).
"""
from __future__ import annotations

import re
from datetime import datetime

from django.conf import settings
from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string
from django.utils.html import escape, linebreaks

from .email_assets import logo_image_src, public_assets_base

BANK_NAME = 'SafaPay Bank'
BANK_TAGLINE = 'Purity, clarity, and trust'

SENDER_DISPLAY_NAMES = {
    'info': 'SafaPay Bank',
    'support': 'SafaPay Support',
    'security': 'SafaPay Security',
    'admin': 'SafaPay Bank',
}

# Outgoing From address per notification type (aliases of the primary mailbox).
EVENT_SENDER_KEYS: dict[str, str | None] = {
    'registration': 'info',
    'transaction': None,  # credit -> info, debit -> security
    'loan_approved': 'info',
    'loan_rejected': 'info',
    'loan_payment_due': 'info',
    'statement_ready': 'info',
    'goal_autosave_success': 'info',
    'goal_autosave_insufficient': 'info',
    'compliance_payment_confirmed': 'info',
    'low_balance': 'info',
    'support_update': 'support',
    'mfa_otp': 'security',
    'compliance_fee_otp': 'security',
    'password_reset': 'security',
    'security_alert': 'security',
    'profile_update_approved': 'admin',
}

# Footer / reply contact may differ from From (e.g. welcome from info@, questions to support@).
EVENT_CONTACT_SENDER_KEYS: dict[str, str | None] = {
    'registration': 'support',
    'loan_approved': 'support',
    'loan_rejected': 'support',
    'loan_payment_due': 'support',
    'compliance_payment_confirmed': 'support',
    'low_balance': 'support',
    'goal_autosave_success': 'support',
    'goal_autosave_insufficient': 'support',
    'statement_ready': 'support',
    'transaction': None,  # credit -> support, debit -> security
    'profile_update_approved': 'admin',
}


def get_sender_addresses() -> dict[str, str]:
    return {
        'info': getattr(settings, 'INFO_EMAIL', 'info@safapaygroup.com'),
        'support': getattr(settings, 'SUPPORT_EMAIL', 'support@safapaygroup.com'),
        'security': getattr(settings, 'SECURITY_EMAIL', 'security@safapaygroup.com'),
        'admin': getattr(settings, 'ADMIN_EMAIL', 'admin@safapaygroup.com'),
    }


def resolve_sender_key(event_type: str, context: dict | None = None) -> str:
    """Pick info / support / security / admin based on notification type."""
    context = context or {}
    if event_type == 'transaction':
        direction = (context.get('direction') or '').lower()
        return 'security' if direction == 'debit' else 'info'
    key = EVENT_SENDER_KEYS.get(event_type)
    return key if key else 'info'


def resolve_contact_key(event_type: str, context: dict | None = None) -> str:
    """Footer mailto / help contact for the email body."""
    context = context or {}
    if event_type == 'transaction':
        direction = (context.get('direction') or '').lower()
        return 'security' if direction == 'debit' else 'support'
    override = EVENT_CONTACT_SENDER_KEYS.get(event_type)
    if override:
        return override
    return resolve_sender_key(event_type, context)


def format_from_address(sender_key: str) -> str:
    addresses = get_sender_addresses()
    key = sender_key if sender_key in addresses else 'info'
    email = addresses[key]
    name = SENDER_DISPLAY_NAMES.get(key, BANK_NAME)
    return f'{name} <{email}>'


def get_from_email_for_event(event_type: str, context: dict | None = None) -> str:
    return format_from_address(resolve_sender_key(event_type, context))


def get_from_email() -> str:
    """Default From header (info@); prefer explicit DEFAULT_FROM_EMAIL when set."""
    raw = (getattr(settings, 'DEFAULT_FROM_EMAIL', '') or '').strip()
    if not raw:
        user = (getattr(settings, 'EMAIL_HOST_USER', '') or '').strip()
        if user:
            return format_from_address('info') if '@' in user else f'{BANK_NAME} <{user}>'
        return format_from_address('info')
    if '@' in raw and '<' not in raw and '>' not in raw:
        return f'{BANK_NAME} <{raw}>'
    return raw


EMAIL_SUBJECTS = {
    'registration': 'Welcome to SafaPay Bank — Your account is ready',
    'password_reset': 'Reset your SafaPay password',
    'mfa_otp': 'Your SafaPay verification code',
    'compliance_fee_otp': 'Your compliance verification code',
    'compliance_payment_confirmed': 'Compliance payment confirmed',
    'transaction': 'Transaction alert — SafaPay Bank',
    'low_balance': 'Low Balance Alert',
    'loan_approved': 'Loan Application Approved',
    'loan_rejected': 'Loan Application Update',
    'loan_payment_due': 'Loan Payment Reminder',
    'statement_ready': 'Your Statement is Ready',
    'support_update': 'Support Ticket Update',
    'security_alert': 'Security Alert — Action Required',
    'profile_update_approved': 'Your profile update was approved',
    'goal_autosave_success': 'Money moved to your savings goal',
    'goal_autosave_insufficient': 'Savings goal — add funds to your account',
}


def fallback_body(event_type: str, context: dict) -> str:
    if event_type == 'mfa_otp':
        label = context.get('otp_validity_label', '5 minutes')
        return f"Your verification code is: {context.get('otp')}. Valid for {label}."
    if event_type == 'compliance_fee_otp':
        hours = context.get('valid_hours', 48)
        fee = context.get('fee_name') or 'compliance fee'
        return (
            f"Your verification code for {fee} is: {context.get('otp')}. "
            f"Valid for {hours} hours. Do not share this code."
        )
    if event_type == 'compliance_payment_confirmed':
        fee = context.get('fee_name') or 'compliance fee'
        return (
            f"We confirmed receipt of your {fee} payment. "
            "You will receive a verification code by email when it is ready."
        )
    if event_type == 'password_reset':
        return f"Click the link to reset your password: {context.get('token')}"
    if event_type == 'transaction':
        return (
            f"Transaction alert: {context.get('tx_type')} of "
            f"{context.get('currency')} {context.get('amount')} "
            f"(Ref: {context.get('reference')})"
        )
    if event_type == 'profile_update_approved':
        return (
            f"Hello {context.get('full_name') or context.get('user')}, "
            'your profile change request was approved and your details are updated.'
        )
    if event_type == 'goal_autosave_success':
        return (
            f"We moved {context.get('amount')} to your goal “{context.get('goal_title')}” "
            f"({context.get('plan_label')}). Saved so far: {context.get('new_saved_balance')}."
        )
    if event_type == 'goal_autosave_insufficient':
        return (
            f"We couldn’t move {context.get('amount')} to “{context.get('goal_title')}” "
            f"({context.get('plan_label')}) — available in your primary account is only "
            f"{context.get('available_balance')}. Add funds to your primary account to keep "
            'this goal on track.'
        )
    if event_type == 'loan_approved':
        product = context.get('product_name') or context.get('loan_type') or 'loan'
        return (
            f"Congratulations! Your {product} application was approved. "
            'Sign in to SafaPay to continue.'
        )
    if event_type == 'support_update':
        return (
            f"Hello {context.get('full_name') or 'there'},\n\n"
            f"We replied to your support ticket #{context.get('ticket_number')} "
            f"({context.get('subject')}).\n\n"
            f"Current status: {context.get('status')}\n\n"
            f"{context.get('staff_reply', '').strip()}\n\n"
            '— SafaPay Bank Support'
        )
    return f"You have a new notification: {event_type}"


def get_frontend_base_url() -> str:
    origins = getattr(settings, 'CORS_ALLOWED_ORIGINS', None) or []
    if isinstance(origins, str):
        origins = [origins]
    if origins:
        return origins[0].strip().rstrip('/')
    return ''


def get_sign_in_url() -> str:
    base = get_frontend_base_url()
    return f'{base}/auth/signin' if base else ''


def get_email_brand_context() -> dict:
    year = datetime.now().year
    # Header logo: HTTPS only when EMAIL_ASSETS_BASE_URL is public; else HTML wordmark (Gmail-safe).
    custom_logo = (getattr(settings, 'EMAIL_LOGO_URL', '') or '').strip()
    logo_src = logo_image_src(custom_logo)
    use_logo_image = bool(logo_src) and (
        custom_logo.startswith('https://') or bool(public_assets_base())
    )

    def _social_url(key: str) -> str:
        return (getattr(settings, key, '') or '').strip() or '#'

    # HTML icon boxes (Gmail-safe) — X, Facebook, LinkedIn only.
    social_icons = [
        {'label': 'X (Twitter)', 'glyph': '𝕏', 'url': _social_url('EMAIL_SOCIAL_TWITTER')},
        {'label': 'Facebook', 'glyph': 'f', 'url': _social_url('EMAIL_SOCIAL_FACEBOOK')},
        {'label': 'LinkedIn', 'glyph': 'in', 'url': _social_url('EMAIL_SOCIAL_LINKEDIN')},
    ]

    return {
        'bank_name': BANK_NAME,
        'bank_tagline': BANK_TAGLINE,
        'support_email': getattr(settings, 'SUPPORT_EMAIL', 'support@safapaygroup.com'),
        'copyright_year': year,
        'logo_src': logo_src if use_logo_image else '',
        'has_logo': use_logo_image,
        'primary_color': '#152a1e',
        'accent_color': '#c8f000',
        'social_icons': social_icons,
        'sign_in_url': get_sign_in_url(),
    }


def plain_text_to_html(text: str) -> str:
    """Turn plain-text email bodies into simple HTML paragraphs."""
    cleaned = text.strip()
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    # Drop legacy per-template sign-offs; global footer is in base.html.
    cleaned = re.sub(
        r'\n*Best regards,?\s*\n*SafaPay Bank Team\s*$',
        '',
        cleaned,
        flags=re.IGNORECASE,
    )
    return linebreaks(escape(cleaned))


def wrap_text_body(inner_text: str, extra_context: dict | None = None) -> str:
    ctx = {**get_email_brand_context(), **(extra_context or {})}
    ctx['email_body'] = inner_text.strip()
    return render_to_string('emails/base.txt', ctx)


def wrap_html_body(inner_html: str, extra_context: dict | None = None) -> str:
    ctx = {**get_email_brand_context(), **(extra_context or {})}
    ctx['email_body'] = inner_html
    return render_to_string('emails/base.html', ctx)


def render_event_email(event_type: str, context: dict) -> tuple[str, str, str]:
    """
    Returns (subject, plain_text_body, html_body) with branded header and footer.
    """
    subject = EMAIL_SUBJECTS.get(event_type, 'SafaPay Bank Notification')
    contact_key = resolve_contact_key(event_type, context)
    ctx = {**get_email_brand_context(), **context, 'email_subject': subject}
    ctx['support_email'] = get_sender_addresses()[contact_key]
    if event_type == 'mfa_otp':
        validity_sec = int(getattr(settings, 'OTP_EMAIL_TOKEN_VALIDITY', 300))
        if validity_sec >= 60:
            ctx['otp_validity_label'] = f'{max(1, validity_sec // 60)} minutes'
        else:
            ctx['otp_validity_label'] = f'{validity_sec} seconds'
    if event_type == 'compliance_fee_otp':
        ctx.setdefault('valid_hours', 48)

    try:
        inner_text = render_to_string(f'emails/{event_type}.txt', ctx)
    except TemplateDoesNotExist:
        inner_text = fallback_body(event_type, context)

    text_body = wrap_text_body(inner_text, ctx)

    try:
        inner_html = render_to_string(f'emails/{event_type}.html', ctx)
    except TemplateDoesNotExist:
        inner_html = plain_text_to_html(inner_text)

    html_body = wrap_html_body(inner_html, ctx)
    return subject, text_body, html_body


def render_custom_email(
    *,
    subject: str,
    text_body: str,
    html_body: str | None = None,
    extra_context: dict | None = None,
) -> tuple[str, str, str]:
    """Wrap arbitrary subject/body (e.g. samples, one-off sends)."""
    ctx = {**get_email_brand_context(), **(extra_context or {}), 'email_subject': subject}
    wrapped_text = wrap_text_body(text_body, ctx)
    inner_html = html_body if html_body is not None else plain_text_to_html(text_body)
    wrapped_html = wrap_html_body(inner_html, ctx)
    return subject, wrapped_text, wrapped_html
