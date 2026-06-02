"""Account provisioning helpers (UAE domestic 16-digit number + IBAN)."""
from __future__ import annotations

from decimal import Decimal
import random

from django.conf import settings

from .models import Account, Currency
from .uae_iban import DOMESTIC_ACCOUNT_NUMBER_LENGTH

UAE_BANK_CODE = getattr(settings, 'UAE_BANK_CODE', '033')


def get_or_create_default_currency() -> Currency:
    """
    Provisioning must never fail for lack of seed data: prefer AED, then any active row,
    then create AED (dev/demo safety net).
    """
    c = Currency.objects.filter(code='AED', is_active=True).first()
    if c:
        return c
    c = Currency.objects.filter(is_active=True).first()
    if c:
        return c
    return Currency.objects.create(code='AED', name='UAE Dirham', symbol='د.إ', is_active=True)


def _mod97_from_numeric_string(num_str: str) -> int:
    r = 0
    for ch in num_str:
        r = (r * 10 + int(ch)) % 97
    return r


def build_ae_iban(bank_code: str, domestic_account_16: str) -> str:
    """
    UAE IBAN is 23 characters: AE + 2 check digits + 3-digit bank code + 16-digit account.
    """
    domestic_account_16 = domestic_account_16.strip()
    bank_code = bank_code.strip()
    if len(domestic_account_16) != 16 or not domestic_account_16.isdigit():
        raise ValueError('Domestic account must be 16 digits')
    if len(bank_code) != 3 or not bank_code.isdigit():
        raise ValueError('Bank code must be 3 digits')
    bban = f'{bank_code}{domestic_account_16}'
    rearranged = bban + 'AE' + '00'
    expanded = []
    for c in rearranged:
        if c.isdigit():
            expanded.append(c)
        else:
            expanded.append(str(ord(c.upper()) - 55))
    num_str = ''.join(expanded)
    remainder = _mod97_from_numeric_string(num_str)
    check = 98 - remainder
    check_digits = f'{check:02d}'
    return f'AE{check_digits}{bban}'


def generate_unique_domestic_account_number() -> str:
    while True:
        candidate = ''.join(str(random.randint(0, 9)) for _ in range(16))
        if not Account.objects.filter(account_number=candidate).exists():
            return candidate


def map_intended_account_type(intended: str) -> str:
    mapping = {
        'SAVINGS': Account.AccountType.SAVINGS,
        'CHECKING': Account.AccountType.CHECKING,
        'BUSINESS': Account.AccountType.BUSINESS,
        'FIXED_TERM': Account.AccountType.FIXED_TERM,
        'CREDIT': Account.AccountType.CREDIT,
    }
    return mapping.get(intended, Account.AccountType.CHECKING)


def _ensure_single_primary(owner_id, primary_pk):
    Account.objects.filter(owner_id=owner_id).exclude(pk=primary_pk).update(is_primary=False)


def provision_primary_bank_account(user) -> Account | None:
    """
    Create the customer's first AED account (16-digit + IBAN) if they have none.
    Otherwise return the primary account (or oldest) and ensure one row is marked primary.
    """
    primary = Account.objects.filter(owner=user, is_primary=True).first()
    if primary:
        return primary

    existing = Account.objects.filter(owner=user).order_by('created_at').first()
    if existing:
        existing.is_primary = True
        existing.save(update_fields=['is_primary'])
        _ensure_single_primary(user.id, existing.id)
        return existing

    currency = get_or_create_default_currency()

    domestic = generate_unique_domestic_account_number()
    iban = build_ae_iban(UAE_BANK_CODE, domestic)
    while Account.objects.filter(iban=iban).exists():
        domestic = generate_unique_domestic_account_number()
        iban = build_ae_iban(UAE_BANK_CODE, domestic)

    acc_type = map_intended_account_type(user.intended_account_type or '')
    account = Account.objects.create(
        owner=user,
        account_number=domestic,
        iban=iban,
        account_type=acc_type,
        currency=currency,
        balance=Decimal('0'),
        available_balance=Decimal('0'),
        is_primary=True,
    )
    _ensure_single_primary(user.id, account.id)
    return account


def create_additional_uae_account(
    owner,
    account_type: str,
    currency: Currency | None,
    nickname: str = '',
    *,
    exclude_from_card_summary: bool = False,
) -> Account:
    """Second and further accounts: 16-digit domestic number + IBAN, never primary by default."""
    if currency is None:
        currency = get_or_create_default_currency()
    domestic = generate_unique_domestic_account_number()
    iban = build_ae_iban(UAE_BANK_CODE, domestic)
    while Account.objects.filter(iban=iban).exists():
        domestic = generate_unique_domestic_account_number()
        iban = build_ae_iban(UAE_BANK_CODE, domestic)
    return Account.objects.create(
        owner=owner,
        account_number=domestic,
        iban=iban,
        account_type=account_type,
        currency=currency,
        nickname=nickname or '',
        balance=Decimal('0'),
        available_balance=Decimal('0'),
        is_primary=False,
        exclude_from_card_summary=exclude_from_card_summary,
    )


def sync_primary_account_type_from_profile(user) -> None:
    """After onboarding, align primary account product type with intended_account_type."""
    intended = (user.intended_account_type or '').strip()
    if not intended:
        return
    primary = Account.objects.filter(owner=user, is_primary=True).first()
    if not primary:
        primary = Account.objects.filter(owner=user).order_by('created_at').first()
    if not primary:
        return
    new_type = map_intended_account_type(intended)
    if primary.account_type != new_type:
        primary.account_type = new_type
        primary.save(update_fields=['account_type'])
