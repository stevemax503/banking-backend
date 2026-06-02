"""Standard depositor/source fields and narration for admin deposits."""
from __future__ import annotations


class TransactionError(Exception):
    pass


class DepositMethod:
    TRANSFER = 'TRANSFER'
    CARD = 'CARD'
    WIRE = 'WIRE'
    CASH = 'CASH'
    CHECK = 'CHECK'
    MOBILE = 'MOBILE'
    OTHER = 'OTHER'

    CHOICES = (
        (TRANSFER, 'Bank transfer'),
        (CARD, 'Card'),
        (WIRE, 'Wire'),
        (CASH, 'Cash'),
        (CHECK, 'Check'),
        (MOBILE, 'Mobile payment'),
        (OTHER, 'Other'),
    )

# (field_key, label, required)
DEPOSIT_SOURCE_FIELDS: dict[str, list[tuple[str, str, bool]]] = {
    DepositMethod.TRANSFER: [
        ('depositor_name', 'Depositor name', True),
        ('sender_bank_name', 'Originating bank', True),
        ('sender_account_number', 'Sender account number', True),
        ('transfer_reference', 'Transfer reference', False),
    ],
    DepositMethod.CARD: [
        ('cardholder_name', 'Cardholder name', True),
        ('card_last_four', 'Card last 4 digits', True),
        ('card_brand', 'Card brand', False),
        ('authorization_code', 'Authorization code', False),
    ],
    DepositMethod.WIRE: [
        ('originator_name', 'Originator name', True),
        ('originator_bank', 'Originator bank', True),
        ('wire_reference', 'Wire reference (IMAD/OMAD)', True),
        ('beneficiary_reference', 'Beneficiary reference', False),
    ],
    DepositMethod.CASH: [
        ('depositor_name', 'Depositor name', True),
        ('branch_location', 'Branch / location', True),
        ('receipt_reference', 'Receipt reference', False),
    ],
    DepositMethod.CHECK: [
        ('payor_name', 'Payor name', True),
        ('check_number', 'Check number', True),
        ('drawee_bank', 'Drawee bank', True),
        ('check_date', 'Check date', False),
    ],
    DepositMethod.MOBILE: [
        ('payer_name', 'Payer name', True),
        ('wallet_provider', 'Wallet / provider', True),
        ('payment_reference', 'Payment reference', True),
    ],
    DepositMethod.OTHER: [
        ('depositor_name', 'Depositor / source name', True),
        ('source_description', 'Source details', True),
    ],
}

METHOD_LABEL = dict(DepositMethod.CHOICES)


def normalize_deposit_source(deposit_method: str, raw: dict | None) -> dict[str, str]:
    if deposit_method not in DEPOSIT_SOURCE_FIELDS:
        raise TransactionError('Invalid deposit method.')
    spec = DEPOSIT_SOURCE_FIELDS[deposit_method]
    src = raw if isinstance(raw, dict) else {}
    out: dict[str, str] = {}
    missing: list[str] = []
    for key, label, required in spec:
        val = str(src.get(key, '') or '').strip()
        if required and not val:
            missing.append(label)
        elif val:
            out[key] = val
    if missing:
        raise TransactionError(f'Missing required deposit details: {", ".join(missing)}.')
    return out


def build_deposit_narration(deposit_method: str, source: dict[str, str]) -> str:
    """Legacy uppercase narration (metadata / mirrors). Prefer narration.build_deposit_system_narration."""
    method = METHOD_LABEL.get(deposit_method, deposit_method.replace('_', ' ')).upper()

    def part(label: str, value: str) -> str:
        return f'{label}: {value}'.upper()

    if deposit_method == DepositMethod.TRANSFER:
        bits = [
            method,
            source.get('depositor_name', ''),
            part('BANK', source.get('sender_bank_name', '')),
            part('FROM ACCT', _mask_account(source.get('sender_account_number', ''))),
        ]
        if source.get('transfer_reference'):
            bits.append(part('REF', source['transfer_reference']))
    elif deposit_method == DepositMethod.CARD:
        bits = [
            method,
            source.get('cardholder_name', ''),
            part('CARD', f"****{source.get('card_last_four', '')}"),
        ]
        if source.get('card_brand'):
            bits.append(source['card_brand'].upper())
        if source.get('authorization_code'):
            bits.append(part('AUTH', source['authorization_code']))
    elif deposit_method == DepositMethod.WIRE:
        bits = [
            method,
            source.get('originator_name', ''),
            part('BANK', source.get('originator_bank', '')),
            part('WIRE REF', source.get('wire_reference', '')),
        ]
        if source.get('beneficiary_reference'):
            bits.append(part('BEN REF', source['beneficiary_reference']))
    elif deposit_method == DepositMethod.CASH:
        bits = [
            method,
            source.get('depositor_name', ''),
            part('LOCATION', source.get('branch_location', '')),
        ]
        if source.get('receipt_reference'):
            bits.append(part('RECEIPT', source['receipt_reference']))
    elif deposit_method == DepositMethod.CHECK:
        bits = [
            method,
            source.get('payor_name', ''),
            part('CHECK', source.get('check_number', '')),
            part('BANK', source.get('drawee_bank', '')),
        ]
        if source.get('check_date'):
            bits.append(part('DATE', source['check_date']))
    elif deposit_method == DepositMethod.MOBILE:
        bits = [
            method,
            source.get('payer_name', ''),
            part('VIA', source.get('wallet_provider', '')),
            part('REF', source.get('payment_reference', '')),
        ]
    else:
        bits = [
            method,
            source.get('depositor_name', ''),
            source.get('source_description', ''),
        ]

    return ' · '.join(b for b in bits if b)


def _mask_account(number: str) -> str:
    digits = ''.join(c for c in number if c.isdigit())
    if len(digits) <= 4:
        return digits
    return f'****{digits[-4:]}'
