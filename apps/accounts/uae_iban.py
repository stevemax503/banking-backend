"""UAE account identifiers: IBAN and domestic account number (11–16 digits)."""
from __future__ import annotations

import re

DOMESTIC_ACCOUNT_NUMBER_MIN_LENGTH = 11
DOMESTIC_ACCOUNT_NUMBER_MAX_LENGTH = 16
# New accounts are provisioned with a 16-digit domestic number.
DOMESTIC_ACCOUNT_NUMBER_LENGTH = DOMESTIC_ACCOUNT_NUMBER_MAX_LENGTH

UAE_IBAN_MIN_LENGTH = 2 + 2 + 3 + DOMESTIC_ACCOUNT_NUMBER_MIN_LENGTH  # 18
UAE_IBAN_MAX_LENGTH = 2 + 2 + 3 + DOMESTIC_ACCOUNT_NUMBER_MAX_LENGTH  # 23
# AE + 2 check + 3 bank + 11–16 account
UAE_IBAN_PATTERN = re.compile(
    rf'^AE\d{{{2 + 3 + DOMESTIC_ACCOUNT_NUMBER_MIN_LENGTH},{2 + 3 + DOMESTIC_ACCOUNT_NUMBER_MAX_LENGTH}}}$',
)


class UaeAccountIdentifierError(ValueError):
    pass


def compact_identifier(raw: str) -> str:
    return re.sub(r'\s+', '', (raw or '').strip()).upper()


def is_valid_domestic_account_number(digits: str) -> bool:
    return digits.isdigit() and DOMESTIC_ACCOUNT_NUMBER_MIN_LENGTH <= len(digits) <= DOMESTIC_ACCOUNT_NUMBER_MAX_LENGTH


def is_valid_uae_iban(compact: str) -> bool:
    return bool(compact and UAE_IBAN_PATTERN.match(compact))


def extract_domestic_from_uae_iban(compact_iban: str) -> str:
    if not is_valid_uae_iban(compact_iban):
        raise UaeAccountIdentifierError('Invalid UAE IBAN.')
    domestic = compact_iban[7:]
    if not is_valid_domestic_account_number(domestic):
        raise UaeAccountIdentifierError('Invalid UAE IBAN.')
    return domestic


def normalize_to_domestic_account_number(raw: str) -> str:
    """Accept UAE IBAN or 11–16 digit domestic account number."""
    compact = compact_identifier(raw)
    if not compact:
        raise UaeAccountIdentifierError('Account number is required.')
    if compact.startswith('AE'):
        return extract_domestic_from_uae_iban(compact)
    digits = re.sub(r'\D', '', compact)
    if is_valid_domestic_account_number(digits):
        return digits
    raise UaeAccountIdentifierError(
        f'Account number must be {DOMESTIC_ACCOUNT_NUMBER_MIN_LENGTH} to {DOMESTIC_ACCOUNT_NUMBER_MAX_LENGTH} digits.',
    )
