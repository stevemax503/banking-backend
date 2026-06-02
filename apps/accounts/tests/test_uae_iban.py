import pytest

from apps.accounts.uae_iban import (
    UaeAccountIdentifierError,
    extract_domestic_from_uae_iban,
    is_valid_domestic_account_number,
    is_valid_uae_iban,
    normalize_to_domestic_account_number,
)
from apps.accounts.services import build_ae_iban


@pytest.mark.unit
class TestUaeIban:
    def test_normalize_from_iban_with_spaces(self):
        domestic = '8230343052496520'
        iban = build_ae_iban('033', domestic)
        spaced = f'{iban[:4]} {iban[4:8]} {iban[8:12]} {iban[12:16]} {iban[16:20]} {iban[20:]}'
        assert normalize_to_domestic_account_number(spaced) == domestic

    def test_normalize_from_16_digits(self):
        assert normalize_to_domestic_account_number('8230343052496520') == '8230343052496520'

    def test_normalize_from_11_digits(self):
        short = '12345678901'
        assert normalize_to_domestic_account_number(short) == short

    def test_rejects_too_short(self):
        with pytest.raises(UaeAccountIdentifierError, match='11 to 16'):
            normalize_to_domestic_account_number('1234567890')

    def test_rejects_too_long(self):
        with pytest.raises(UaeAccountIdentifierError, match='11 to 16'):
            normalize_to_domestic_account_number('12345678901234567')

    def test_is_valid_uae_iban(self):
        iban = build_ae_iban('033', '8230343052496520')
        assert is_valid_uae_iban(iban)
        assert extract_domestic_from_uae_iban(iban) == '8230343052496520'

    def test_domestic_length_bounds(self):
        assert is_valid_domestic_account_number('1' * 11)
        assert is_valid_domestic_account_number('9' * 16)
        assert not is_valid_domestic_account_number('1' * 10)
        assert not is_valid_domestic_account_number('9' * 17)
