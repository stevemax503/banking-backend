"""Normalize and validate SWIFT MT103-style international transfer details (creditor + creditor agent + 71A + refs)."""
import re

from rest_framework.exceptions import ValidationError

TRANSFER_INTERNATIONAL = 'TRANSFER_INTERNATIONAL'

CHARGES_OPTIONS = frozenset({'SHA', 'BEN', 'OUR'})

# (key, min_len, max_len) — required text fields
REQUIRED_STRING_FIELDS = [
    ('beneficiary_legal_name', 2, 140),
    ('beneficiary_address_line1', 2, 140),
    ('beneficiary_city', 2, 80),
    ('beneficiary_postal_code', 1, 16),
    ('beneficiary_bank_name', 2, 120),
    ('beneficiary_bank_address_line1', 2, 140),
    ('beneficiary_bank_city', 2, 80),
    ('purpose_of_payment', 2, 140),
]

# (key, max_len) — optional; included in output only when non-empty after strip
OPTIONAL_STRING_FIELDS = [
    ('remittance_reference', 35),
    ('beneficiary_address_line2', 140),
    ('beneficiary_region_state', 80),
    ('beneficiary_bank_address_line2', 140),
    ('intermediary_bank_name', 120),
    ('instructions_to_beneficiary_bank', 140),
]


def _bic_core(raw: str) -> str:
    s = re.sub(r'\s+', '', (raw or '')).upper()
    if not re.fullmatch(r'[A-Z0-9]{8}|[A-Z0-9]{11}', s):
        raise ValueError('SWIFT/BIC must be 8 or 11 letters and digits (no spaces).')
    return s


def _iban_core(raw: str) -> str:
    s = re.sub(r'\s+', '', (raw or '')).upper()
    if not re.fullmatch(r'[A-Z0-9]{15,34}', s):
        raise ValueError('IBAN must be 15–34 letters and digits (spaces allowed when typing).')
    return s


def _iso2(raw: str) -> str:
    c = (raw or '').strip().upper()
    if not re.fullmatch(r'[A-Z]{2}', c):
        raise ValueError('Use a 2-letter ISO 3166-1 alpha-2 country code.')
    return c


def _postal_core(raw: str) -> str:
    t = (raw or '').strip().upper()
    if not re.fullmatch(r'[A-Z0-9][A-Z0-9 \-]{0,14}[A-Z0-9]|[A-Z0-9]{1}', t):
        raise ValueError('Postal / ZIP code is invalid (letters, digits, spaces, hyphens; max 16 characters).')
    return re.sub(r'\s+', ' ', t).strip()


def validate_and_normalize_international_details(raw) -> dict:
    """
    Returns a stable dict for JSON storage and equality checks (MT103-aligned field set).
    Raises DRF ValidationError with field paths under international_details.*.
    """
    if raw is None:
        raise ValidationError({'international_details': 'This field is required for international transfers.'})
    if not isinstance(raw, dict):
        raise ValidationError({'international_details': 'Must be a JSON object.'})

    errors: dict[str, str] = {}

    for key, min_len, max_len in REQUIRED_STRING_FIELDS:
        val = raw.get(key)
        if val is None or not isinstance(val, str):
            errors[key] = 'Required.'
            continue
        t = val.strip()
        if len(t) < min_len:
            errors[key] = 'This field is required.' if not t else f'Must be at least {min_len} characters.'
        elif len(t) > max_len:
            errors[key] = f'Must be at most {max_len} characters.'

    co = raw.get('charges_option')
    if co is None or not isinstance(co, str):
        errors['charges_option'] = 'Required. Choose SHA, BEN, or OUR (MT103 field 71A).'
    else:
        c = co.strip().upper()
        if c not in CHARGES_OPTIONS:
            errors['charges_option'] = 'Must be one of: SHA (shared), BEN (beneficiary pays), OUR (sender pays all).'

    for key, max_len in OPTIONAL_STRING_FIELDS:
        val = raw.get(key)
        if val is None or val == '':
            continue
        if not isinstance(val, str):
            errors[key] = 'Must be a string.'
            continue
        t = val.strip()
        if key == 'remittance_reference' and len(t) < 2:
            errors[key] = 'Must be at least 2 characters when provided.'
            continue
        if len(t) > max_len:
            errors[key] = f'Must be at most {max_len} characters.'

    bic = None
    bic_in = raw.get('beneficiary_bic_swift')
    try:
        bic = _bic_core(bic_in if isinstance(bic_in, str) else '')
    except ValueError as e:
        errors['beneficiary_bic_swift'] = str(e)

    iban = None
    iban_in = raw.get('beneficiary_iban')
    try:
        iban = _iban_core(iban_in if isinstance(iban_in, str) else '')
    except ValueError as e:
        errors['beneficiary_iban'] = str(e)

    bc_country = None
    bb_country = None
    postal = None
    try:
        bc_country = _iso2(raw.get('beneficiary_country', ''))
    except ValueError as e:
        errors['beneficiary_country'] = str(e)
    try:
        bb_country = _iso2(raw.get('beneficiary_bank_country', ''))
    except ValueError as e:
        errors['beneficiary_bank_country'] = str(e)
    try:
        postal = _postal_core(raw.get('beneficiary_postal_code', '') if isinstance(raw.get('beneficiary_postal_code'), str) else '')
    except ValueError as e:
        errors['beneficiary_postal_code'] = str(e)

    intermediary = None
    raw_im = raw.get('intermediary_bank_bic')
    if raw_im is not None and str(raw_im).strip():
        try:
            intermediary = _bic_core(str(raw_im))
        except ValueError as e:
            errors['intermediary_bank_bic'] = str(e)

    if errors:
        raise ValidationError({'international_details': errors})

    assert bic is not None and iban is not None and bc_country and bb_country and postal

    charges = (raw.get('charges_option') or '').strip().upper()

    out: dict = {
        'beneficiary_legal_name': raw['beneficiary_legal_name'].strip(),
        'beneficiary_address_line1': raw['beneficiary_address_line1'].strip(),
        'beneficiary_city': raw['beneficiary_city'].strip(),
        'beneficiary_postal_code': postal,
        'beneficiary_country': bc_country,
        'beneficiary_bank_name': raw['beneficiary_bank_name'].strip(),
        'beneficiary_bank_address_line1': raw['beneficiary_bank_address_line1'].strip(),
        'beneficiary_bank_city': raw['beneficiary_bank_city'].strip(),
        'beneficiary_bank_country': bb_country,
        'beneficiary_bic_swift': bic,
        'beneficiary_iban': iban,
        'purpose_of_payment': raw['purpose_of_payment'].strip(),
        'charges_option': charges,
    }

    for key, max_len in OPTIONAL_STRING_FIELDS:
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()

    if intermediary:
        out['intermediary_bank_bic'] = intermediary

    return out
