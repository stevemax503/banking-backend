import pytest

from apps.loans.loan_application_pdf import LoanFormPrefill, generate_loan_application_pdf


@pytest.mark.django_db
def test_generate_loan_application_pdf_returns_bytes():
    pdf = generate_loan_application_pdf(
        LoanFormPrefill(
            applicant_name='Jane Doe',
            email='jane@example.com',
            loan_amount='50000.00',
            loan_term='36 months',
        )
    )
    assert isinstance(pdf, bytes)
    assert pdf[:4] == b'%PDF'
    assert len(pdf) > 5000
