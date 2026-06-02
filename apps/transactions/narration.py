"""Customer-facing transaction descriptions with reference numbers."""
from __future__ import annotations

import re

from .deposit_source import DepositMethod, METHOD_LABEL

MAX_DESCRIPTION_LEN = 255


def format_transaction_description(
    system: str,
    reference_number: str,
    user_memo: str = '',
) -> str:
    """
    Build display description: {system}{reference} ({memo}).
    Reference is appended directly after the system text (no extra space).
    """
    base = (system or '').strip()
    ref = (reference_number or '').strip()
    if ref and ref not in base:
        base = f'{base}{ref}'
    memo = (user_memo or '').strip()
    if memo:
        memo_clean = memo.strip()
        if memo_clean.startswith('(') and memo_clean.endswith(')'):
            base = f'{base} {memo_clean}'
        else:
            base = f'{base} ({memo_clean})'
    return base[:MAX_DESCRIPTION_LEN]


def split_user_memo(description: str) -> str:
    """Extract trailing parenthetical memo from a combined description."""
    text = (description or '').strip()
    if not text:
        return ''
    m = re.search(r'\s*\(([^()]*)\)\s*$', text)
    return m.group(1).strip() if m else ''


def normalize_deposit_source_optional(deposit_method: str, raw: dict | None) -> dict[str, str]:
    """Admin deposit: all source fields optional; only non-empty values kept."""
    from .deposit_source import DEPOSIT_SOURCE_FIELDS

    if deposit_method not in DEPOSIT_SOURCE_FIELDS:
        return {}
    spec = DEPOSIT_SOURCE_FIELDS[deposit_method]
    src = raw if isinstance(raw, dict) else {}
    out: dict[str, str] = {}
    for key, _label, _required in spec:
        val = str(src.get(key, '') or '').strip()
        if val:
            out[key] = val
    return out


def build_deposit_system_narration(deposit_method: str, source: dict[str, str]) -> str:
    """Sentence-style deposit line (without reference or memo)."""
    src = source or {}

    if deposit_method == DepositMethod.TRANSFER:
        name = src.get('depositor_name', '')
        bank = src.get('sender_bank_name', '')
        if name and bank:
            return f'Transfer from {name}, {bank}'
        if name:
            return f'Transfer from {name}'
        if bank:
            return f'Transfer from {bank}'
        return 'Incoming bank transfer'

    if deposit_method == DepositMethod.WIRE:
        name = src.get('originator_name', '')
        bank = src.get('originator_bank', '')
        if name and bank:
            return f'Wire from {name}, {bank}'
        if name:
            return f'Wire from {name}'
        return 'Incoming wire transfer'

    if deposit_method == DepositMethod.CARD:
        name = src.get('cardholder_name', '')
        last4 = src.get('card_last_four', '')
        if name and last4:
            return f'Card deposit from {name}, card ****{last4}'
        if name:
            return f'Card deposit from {name}'
        return 'Card deposit'

    if deposit_method == DepositMethod.CASH:
        name = src.get('depositor_name', '')
        loc = src.get('branch_location', '')
        if name and loc:
            return f'Cash deposit from {name}, {loc}'
        if name:
            return f'Cash deposit from {name}'
        return 'Cash deposit'

    if deposit_method == DepositMethod.CHECK:
        payor = src.get('payor_name', '')
        bank = src.get('drawee_bank', '')
        chk = src.get('check_number', '')
        if payor and bank and chk:
            return f'Check deposit from {payor}, check {chk}, {bank}'
        if payor and bank:
            return f'Check deposit from {payor}, {bank}'
        if payor:
            return f'Check deposit from {payor}'
        return 'Check deposit'

    if deposit_method == DepositMethod.MOBILE:
        name = src.get('payer_name', '')
        provider = src.get('wallet_provider', '')
        if name and provider:
            return f'Mobile payment from {name}, {provider}'
        if name:
            return f'Mobile payment from {name}'
        return 'Mobile payment'

    name = src.get('depositor_name', '') or src.get('source_description', '')
    if name:
        return f'Deposit from {name}'
    label = METHOD_LABEL.get(deposit_method, 'Deposit')
    return f'{label} deposit'


def _mask_tail(account_number: str) -> str:
    digits = ''.join(c for c in (account_number or '') if c.isdigit())
    if len(digits) <= 4:
        return digits or '????'
    return f'····{digits[-4:]}'


def build_transfer_out_system_narration(
    transfer_type: str,
    *,
    beneficiary_name: str = '',
    bank_name: str = '',
    destination_account_number: str = '',
    to_account_holder: str = '',
    to_account_number: str = '',
) -> str:
    """Sentence-style outbound transfer line (without reference or memo)."""
    name = (beneficiary_name or to_account_holder or '').strip()
    bank = (bank_name or '').strip()
    dest = (destination_account_number or to_account_number or '').strip()
    tail = _mask_tail(dest) if dest else ''

    if transfer_type == 'TRANSFER_INTERNAL':
        if name and tail:
            return f'Transfer to {name}, SafaPay {tail}'
        if name:
            return f'Transfer to {name}'
        if tail:
            return f'Transfer to SafaPay account {tail}'
        return 'Internal transfer'

    if transfer_type == 'TRANSFER_INTERNATIONAL':
        if name and bank:
            return f'International transfer to {name}, {bank}'
        if name:
            return f'International transfer to {name}'
        if bank:
            return f'International transfer to {bank}'
        return 'International transfer'

    if name and bank:
        return f'Transfer to {name}, {bank}'
    if name:
        return f'Transfer to {name}'
    if bank:
        return f'Transfer to {bank}'
    return 'External transfer'


def build_fee_system_narration(parent_reference: str) -> str:
    return f'Service fee for {parent_reference}'


def build_reversal_system_narration(original_reference: str, original_description: str = '') -> str:
    orig = (original_description or '').strip()
    ref = (original_reference or '').strip()
    if orig and ref and ref in orig:
        return f'Reversal of {orig}'
    if ref:
        return f'Reversal of {ref}'
    return 'Reversal'


def finalize_transaction_description(
    tx,
    system: str,
    user_memo: str = '',
    *,
    save: bool = True,
) -> str:
    desc = format_transaction_description(system, tx.reference_number, user_memo)
    if save:
        tx.description = desc
        tx.save(update_fields=['description'])
    return desc
