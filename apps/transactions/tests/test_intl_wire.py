import pytest
from rest_framework.exceptions import ValidationError

from apps.transactions.intl_wire import validate_and_normalize_international_details


def _full_raw(**overrides):
    base = {
        'beneficiary_legal_name': 'Jane Recipient',
        'beneficiary_address_line1': '1 Main Street',
        'beneficiary_city': 'Berlin',
        'beneficiary_postal_code': '10115',
        'beneficiary_country': 'DE',
        'beneficiary_bank_name': 'Global Bank SA',
        'beneficiary_bank_address_line1': 'Friedrichstraße 100',
        'beneficiary_bank_city': 'Berlin',
        'beneficiary_bank_country': 'DE',
        'beneficiary_bic_swift': 'DEUTDEFF',
        'beneficiary_iban': 'DE89 3704 0044 0532 0130 00',
        'purpose_of_payment': 'Invoice 2024-17 family support',
        'remittance_reference': 'INV-2024-17',
        'charges_option': 'SHA',
    }
    base.update(overrides)
    return base


@pytest.mark.unit
class TestInternationalWireValidation:
    def test_valid_minimal(self):
        out = validate_and_normalize_international_details(_full_raw())
        assert out['beneficiary_bic_swift'] == 'DEUTDEFF'
        assert out['beneficiary_iban'] == 'DE89370400440532013000'
        assert out['beneficiary_country'] == 'DE'
        assert out['charges_option'] == 'SHA'
        assert out['remittance_reference'] == 'INV-2024-17'
        assert 'intermediary_bank_bic' not in out

    def test_optional_address_and_intermediary(self):
        raw = _full_raw(
            beneficiary_address_line2='Building C',
            beneficiary_region_state='BE',
            beneficiary_bank_address_line2='Floor 3',
            intermediary_bank_bic=' BOFAUS3N ',
            intermediary_bank_name='Bank of America N.A.',
            instructions_to_beneficiary_bank='Urgent tuition payment',
        )
        out = validate_and_normalize_international_details(raw)
        assert out['beneficiary_address_line2'] == 'Building C'
        assert out['beneficiary_region_state'] == 'BE'
        assert out['intermediary_bank_bic'] == 'BOFAUS3N'
        assert out['intermediary_bank_name'] == 'Bank of America N.A.'
        assert out['instructions_to_beneficiary_bank'] == 'Urgent tuition payment'

    def test_optional_remittance_reference(self):
        raw = _full_raw()
        del raw['remittance_reference']
        out = validate_and_normalize_international_details(raw)
        assert 'remittance_reference' not in out

    def test_missing_bank_country(self):
        raw = _full_raw()
        del raw['beneficiary_bank_country']
        with pytest.raises(ValidationError):
            validate_and_normalize_international_details(raw)

    def test_bad_charges_option(self):
        with pytest.raises(ValidationError):
            validate_and_normalize_international_details(_full_raw(charges_option='XXX'))

    def test_missing_iban(self):
        raw = _full_raw()
        del raw['beneficiary_iban']
        with pytest.raises(ValidationError) as ei:
            validate_and_normalize_international_details(raw)
        assert 'international_details' in ei.value.detail

    def test_bad_bic_length(self):
        with pytest.raises(ValidationError):
            validate_and_normalize_international_details(_full_raw(beneficiary_bic_swift='SHORT'))
