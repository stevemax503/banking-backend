"""Transaction description formatting."""
from apps.transactions.narration import (
    build_deposit_system_narration,
    build_reversal_system_narration,
    format_transaction_description,
)


def test_format_description_glues_reference_and_memo():
    out = format_transaction_description(
        'Transfer from Dan Smith, Citibank',
        'TXN53676674GT',
        'School fees',
    )
    assert out == 'Transfer from Dan Smith, CitibankTXN53676674GT (School fees)'


def test_deposit_system_narration():
    narr = build_deposit_system_narration(
        'TRANSFER',
        {'depositor_name': 'Dan Smith', 'sender_bank_name': 'Citibank'},
    )
    assert narr == 'Transfer from Dan Smith, Citibank'


def test_reversal_includes_reference():
    narr = build_reversal_system_narration('TXN6916161219', 'Transfer to Bob')
    assert 'TXN6916161219' in narr
