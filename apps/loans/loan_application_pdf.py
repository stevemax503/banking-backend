"""SafaPay-branded project loan funding application (7-page PDF)."""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Iterable
from xml.sax.saxutils import escape

from django.conf import settings
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from apps.users.models import CustomUser

# SafaPay brand (matches frontend tailwind + emails)
PRIMARY_DARK = colors.HexColor('#152A1E')
PRIMARY = colors.HexColor('#1E3A2A')
PRIMARY_LIGHT = colors.HexColor('#2D5040')
ACCENT = colors.HexColor('#C8F000')
BLACK = colors.black
GREY = colors.HexColor('#4b5563')
LINE_GREY = colors.HexColor('#9ca3af')
WHITE = colors.white
RED = colors.HexColor('#b91c1c')

BANK_NAME = 'SafaPay Bank'
BANK_SHORT = 'SafaPay'
BANK_TAGLINE = 'Purity, clarity, and trust'
CONTENT_WIDTH = 6.9  # inches (letter minus side margins)


@dataclass
class LoanFormPrefill:
    applicant_name: str = ''
    business_name: str = ''
    email: str = ''
    phone: str = ''
    address: str = ''
    loan_amount: str = ''
    loan_term: str = ''
    loan_purpose: str = ''


def prefill_from_user(user: CustomUser | None) -> LoanFormPrefill:
    if not user:
        return LoanFormPrefill()
    return LoanFormPrefill(
        applicant_name=user.full_name or '',
        email=user.email or '',
        phone=user.phone or '',
        address=user.address or '',
    )


def _office_lines() -> list[tuple[str, str]]:
    custom = getattr(settings, 'LOAN_FORM_OFFICE_LINES', None)
    if custom:
        return list(custom)
    support = getattr(settings, 'SUPPORT_EMAIL', 'support@safapaygroup.com')
    phone = getattr(settings, 'STATEMENT_SUPPORT_PHONE', '') or '1-800-SAFA-PAY'
    return [
        ('Head office', f'{BANK_NAME}\nGlobal operations · Digital-first banking\n{support}'),
        ('Customer support', f'{phone}\n{BANK_TAGLINE}'),
    ]


def _draw_header_footer(canvas, _doc):
    canvas.saveState()
    w, h = letter
    left_m = 0.55 * inch
    right_m = w - 0.55 * inch
    top_y = h - 0.48 * inch

    canvas.setFillColor(PRIMARY_DARK)
    canvas.setFont('Helvetica-Bold', 7)
    y = top_y
    for title, body in _office_lines():
        canvas.drawString(left_m, y, title)
        y -= 9
        canvas.setFont('Helvetica', 6.5)
        for line in body.split('\n'):
            canvas.drawString(left_m, y, line)
            y -= 8
        y -= 4
        canvas.setFont('Helvetica-Bold', 7)

    logo_x = right_m - 1.55 * inch
    logo_y = h - 0.95 * inch
    canvas.setFillColor(PRIMARY_DARK)
    canvas.roundRect(logo_x, logo_y, 1.55 * inch, 0.42 * inch, 4, fill=1, stroke=0)
    canvas.setFillColor(ACCENT)
    canvas.rect(logo_x, logo_y, 0.12 * inch, 0.42 * inch, fill=1, stroke=0)
    canvas.setFillColor(WHITE)
    canvas.setFont('Helvetica-Bold', 11)
    canvas.drawString(logo_x + 0.2 * inch, logo_y + 0.18 * inch, BANK_SHORT)
    canvas.setFillColor(ACCENT)
    canvas.setFont('Helvetica-Oblique', 6)
    canvas.drawString(logo_x + 0.2 * inch, logo_y + 0.06 * inch, BANK_TAGLINE[:28])

    canvas.setStrokeColor(ACCENT)
    canvas.setLineWidth(1.5)
    canvas.line(left_m, h - 1.05 * inch, right_m, h - 1.05 * inch)

    foot_h = 0.28 * inch
    canvas.setFillColor(PRIMARY_DARK)
    canvas.rect(0, 0, w, foot_h, fill=1, stroke=0)
    canvas.setFillColor(ACCENT)
    canvas.rect(0, foot_h, w, 0.04 * inch, fill=1, stroke=0)

    canvas.setFillColor(WHITE)
    canvas.setFont('Helvetica-Bold', 7)
    canvas.drawString(left_m, 0.1 * inch, BANK_SHORT)
    canvas.setFont('Helvetica', 7)
    canvas.drawCentredString(w / 2, 0.1 * inch, getattr(settings, 'SUPPORT_EMAIL', 'support@safapaygroup.com'))
    canvas.setFont('Helvetica-Bold', 7)
    canvas.drawRightString(right_m, 0.1 * inch, f'Page {canvas.getPageNumber()}')

    canvas.restoreState()


def _styles():
    styles = getSampleStyleSheet()
    return {
        'title': ParagraphStyle(
            'title',
            parent=styles['Normal'],
            fontSize=14,
            leading=17,
            fontName='Helvetica-Bold',
            textColor=BLACK,
            alignment=1,
            spaceAfter=10,
        ),
        'section': ParagraphStyle(
            'section',
            parent=styles['Normal'],
            fontSize=10,
            leading=13,
            fontName='Helvetica-Bold',
            textColor=PRIMARY_DARK,
            spaceBefore=4,
            spaceAfter=8,
        ),
        'body': ParagraphStyle(
            'body',
            parent=styles['Normal'],
            fontSize=8.5,
            leading=11,
            fontName='Helvetica',
            textColor=BLACK,
            spaceAfter=4,
        ),
        'label': ParagraphStyle(
            'label',
            parent=styles['Normal'],
            fontSize=8.5,
            leading=11,
            fontName='Helvetica-Bold',
            textColor=BLACK,
            spaceAfter=2,
        ),
        'warn': ParagraphStyle(
            'warn',
            parent=styles['Normal'],
            fontSize=9,
            leading=12,
            fontName='Helvetica-Bold',
            textColor=RED,
            spaceAfter=6,
        ),
        'small': ParagraphStyle(
            'small',
            parent=styles['Normal'],
            fontSize=7.5,
            leading=10,
            fontName='Helvetica',
            textColor=GREY,
            spaceAfter=4,
        ),
        'item': ParagraphStyle(
            'item',
            parent=styles['Normal'],
            fontSize=8.5,
            leading=11,
            fontName='Helvetica',
            textColor=BLACK,
            leftIndent=10,
            spaceAfter=3,
        ),
        'table_cell': ParagraphStyle(
            'table_cell',
            parent=styles['Normal'],
            fontSize=8,
            leading=10,
            fontName='Helvetica',
            textColor=BLACK,
        ),
        'checkbox_q': ParagraphStyle(
            'checkbox_q',
            parent=styles['Normal'],
            fontSize=8,
            leading=10,
            fontName='Helvetica',
            textColor=BLACK,
        ),
    }


def _underline(width: float = CONTENT_WIDTH, indent: float = 0) -> Table:
    tbl = Table([['']], colWidths=[width * inch], rowHeights=[0.18 * inch])
    tbl.setStyle(
        TableStyle(
            [
                ('LINEBELOW', (0, 0), (-1, -1), 0.5, LINE_GREY),
                ('LEFTPADDING', (0, 0), (-1, -1), indent),
                ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                ('TOPPADDING', (0, 0), (-1, -1), 0),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
            ]
        )
    )
    return tbl


def _field_line(label: str, value: str = '', indent: float = 0) -> list:
    """Label on its own line; value or full-width underline beneath."""
    st = _styles()
    label_style = ParagraphStyle(
        'field_label',
        parent=st['label'],
        leftIndent=indent,
    )
    flow: list = [Paragraph(escape(label), label_style)]
    if value:
        value_style = ParagraphStyle(
            'field_value',
            parent=st['body'],
            leftIndent=indent,
            fontName='Helvetica',
        )
        flow.append(Paragraph(escape(value), value_style))
    else:
        flow.append(_underline(CONTENT_WIDTH, indent))
    flow.append(Spacer(1, 0.05 * inch))
    return flow


def _inline_field_row(label: str, value: str = '', label_width: float = 0.55) -> list:
    """Short label and underline on the same row (e.g. $ amount lines)."""
    st = _styles()
    label_style = ParagraphStyle('inline_l', parent=st['label'], spaceAfter=0)
    if value:
        row = Table(
            [[Paragraph(escape(label), label_style), Paragraph(escape(value), st['body'])]],
            colWidths=[label_width * inch, (CONTENT_WIDTH - label_width) * inch],
        )
        row.setStyle(
            TableStyle(
                [
                    ('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                    ('TOPPADDING', (0, 0), (-1, -1), 2),
                    ('LEFTPADDING', (0, 0), (-1, -1), 0),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                ]
            )
        )
    else:
        row = Table(
            [[Paragraph(escape(label), label_style), '']],
            colWidths=[label_width * inch, (CONTENT_WIDTH - label_width) * inch],
        )
        row.setStyle(
            TableStyle(
                [
                    ('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
                    ('LINEBELOW', (1, 0), (1, 0), 0.5, LINE_GREY),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                    ('TOPPADDING', (0, 0), (-1, -1), 2),
                    ('LEFTPADDING', (0, 0), (-1, -1), 0),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                ]
            )
        )
    return [row, Spacer(1, 0.05 * inch)]


def _multiline_box(label: str, value: str = '', height_rows: int = 3) -> list:
    st = _styles()
    flow = [Paragraph(escape(label), st['label'])]
    if value:
        flow.append(Paragraph(escape(value).replace('\n', '<br/>'), st['body']))
    else:
        box = Table(
            [[''] * 1] * height_rows,
            colWidths=[CONTENT_WIDTH * inch],
            rowHeights=[0.22 * inch] * height_rows,
        )
        box.setStyle(
            TableStyle(
                [
                    ('BOX', (0, 0), (-1, -1), 0.75, LINE_GREY),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('LEFTPADDING', (0, 0), (-1, -1), 4),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                ]
            )
        )
        flow.append(box)
    flow.append(Spacer(1, 0.06 * inch))
    return flow


def _checkbox_row(label: str) -> Table:
    st = _styles()
    return Table(
        [
            [
                Paragraph('☐', st['body']),
                Paragraph(escape(label), st['checkbox_q']),
                Paragraph('☐ Yes', st['body']),
                Paragraph('☐ No', st['body']),
            ]
        ],
        colWidths=[0.22 * inch, 5.08 * inch, 0.55 * inch, 0.55 * inch],
        style=TableStyle(
            [
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('ALIGN', (2, 0), (3, 0), 'RIGHT'),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                ('TOPPADDING', (0, 0), (-1, -1), 2),
                ('LEFTPADDING', (0, 0), (-1, -1), 0),
                ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ]
        ),
    )


def _signature_block(n: int) -> list:
    st = _styles()
    lbl = st['label']
    flow: list = [
        Paragraph(escape(f'{n}. Signature(s):'), lbl),
        _underline(),
        Spacer(1, 0.04 * inch),
    ]
    sig_row = Table(
        [
            [
                Paragraph('Name:', lbl),
                '',
                Paragraph('Position:', lbl),
                '',
                Paragraph('Date:', lbl),
                '',
            ]
        ],
        colWidths=[0.48 * inch, 2.2 * inch, 0.62 * inch, 2.2 * inch, 0.4 * inch, 1.0 * inch],
    )
    sig_row.setStyle(
        TableStyle(
            [
                ('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
                ('LINEBELOW', (1, 0), (1, 0), 0.5, LINE_GREY),
                ('LINEBELOW', (3, 0), (3, 0), 0.5, LINE_GREY),
                ('LINEBELOW', (5, 0), (5, 0), 0.5, LINE_GREY),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 2),
                ('LEFTPADDING', (0, 0), (-1, -1), 0),
                ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ]
        )
    )
    flow.extend([sig_row, Spacer(1, 0.12 * inch)])
    return flow


def _bullet_list(items: Iterable[str]) -> list:
    st = _styles()
    return [Paragraph(f'• {escape(item)}', st['item']) for item in items]


def _page1_cover() -> list:
    st = _styles()
    flow = [
        Spacer(1, 0.35 * inch),
        Paragraph('PROJECT LOAN FUNDING APPLICATION', st['title']),
        Spacer(1, 0.12 * inch),
        Paragraph('Please read the following carefully', st['warn']),
        Paragraph(
            '<b>IT IS IMPORTANT THAT YOU PROVIDE THE FOLLOWING INFORMATION AND DOCUMENTS FOR US.</b>',
            st['body'],
        ),
        Paragraph(
            'If you submit without a fully completed application form and all supporting documents '
            'we will not be able to help you.',
            st['body'],
        ),
        Paragraph(
            'This information is required to help us deal with your application speedily and efficiently. '
            'If it is apparent that information has been withheld or appears to be false, your application '
            'will not be considered.',
            st['body'],
        ),
        Spacer(1, 0.08 * inch),
        Paragraph('<b>Required documents</b>', st['body']),
    ]
    flow.extend(
        _bullet_list(
            [
                'Business plan / executive summary',
                'Financial statement articles',
                'Articles of incorporation and by-laws / other similar documents',
                "Proof of identity: a valid international passport copy or driving licence of the company's director",
                'Letter of request',
                'Other information. We need to know what you need the loan for. Please be specific on the '
                'application form. Estimates / quotes will be required.',
            ]
        )
    )
    flow.append(
        Paragraph(
            f'Questions? Contact {getattr(settings, "SUPPORT_EMAIL", "support@safapaygroup.com")}.',
            st['small'],
        )
    )
    return flow


def _page2_section_a(prefill: LoanFormPrefill) -> list:
    st = _styles()
    flow = [
        Spacer(1, 0.35 * inch),
        Paragraph('SECTION A — APPLICATION DETAILS', st['section']),
        *_field_line('1. Applicant name(s):', prefill.applicant_name),
        *_field_line('2. Business name:', prefill.business_name or prefill.applicant_name),
        *_multiline_box('3. Business address:', prefill.address, 2),
        *_field_line('4. Business telephone & fax number:', prefill.phone),
        *_field_line('5. E-mail address:', prefill.email),
        Paragraph(
            '6. Business type (please tick as appropriate):<br/>'
            '☐ a) Sole trader &nbsp;&nbsp;&nbsp; ☐ b) Limited company &nbsp;&nbsp;&nbsp; '
            '☐ c) Partnership &nbsp;&nbsp;&nbsp; ☐ d) Other',
            st['body'],
        ),
        Spacer(1, 0.04 * inch),
        *_field_line('7. Company registered number (if applicable):'),
        *_field_line('8. Date business was established:'),
        *_field_line('9. Accounting year end:'),
        *_multiline_box('10. Brief description of what business does:'),
        Paragraph(
            '<i>If the business is a partnership, please complete section 11.</i>',
            st['small'],
        ),
        *_field_line('11a. Partner name:'),
        *_field_line('Equity held:', indent=14),
        *_field_line('11b. Partner name:'),
        *_field_line('Equity held:', indent=14),
        *_field_line('Others:'),
    ]
    return flow


def _page3_section_b(prefill: LoanFormPrefill) -> list:
    st = _styles()
    flow = [
        Spacer(1, 0.35 * inch),
        Paragraph('SECTION B — GENERAL INFORMATION ABOUT LOAN REQUESTED', st['section']),
        *_multiline_box(
            '1. Purpose of loan — why are you applying (brief description of the project / purpose)?',
            prefill.loan_purpose,
            4,
        ),
        *_field_line('2. Total cost of project (to match A below):'),
        *_field_line('3. Loan amount requested (to match B below):', prefill.loan_amount),
        *_field_line('4. Over what period? (duration of loan requested):', prefill.loan_term),
        *_field_line('5. Do you wish to repay by monthly or quarterly instalment?'),
        *_field_line('6. Do you require a capital repayment holiday? Yes / No'),
        Paragraph('7. Detailed project costs', st['label']),
        *_inline_field_row('$', label_width=0.35),
        *_inline_field_row('$', label_width=0.35),
        Paragraph(
            'Equipment / machinery · working capital · other items (please specify)',
            st['small'],
        ),
        *_field_line('Total cost of project (A) less loan from SafaPay Bank (B):'),
        *_inline_field_row('Total of non-SafaPay Bank funding (C): $', label_width=3.8),
        *_field_line('8. What are the sources of non-SafaPay Bank funding?'),
        *_field_line('Your own financial contribution:', indent=12),
        *_field_line('Bank loan / overdraft:', indent=12),
        *_field_line('HP / Leasing:', indent=12),
        *_field_line('Grants:', indent=12),
    ]
    return flow


def _page4_section_c() -> list:
    st = _styles()
    flow = [
        Spacer(1, 0.35 * inch),
        Paragraph('SECTION C — FINANCIAL RESULTS', st['section']),
        Paragraph('Historic accounts — last 2 months *', st['label']),
        *_field_line('Month ending:'),
        *_field_line('Month ending:'),
        *_field_line('Sales:'),
        *_field_line('Gross profits:'),
        *_field_line('Overheads:'),
        *_field_line('Net profit before interest, tax & drawings (see 3 below):'),
        Spacer(1, 0.04 * inch),
        *_field_line('Fixed assets:'),
        *_field_line('Plus stock:'),
        *_field_line('Plus debtors & payments:'),
        *_field_line('Plus cash:'),
        *_field_line('Total assets:'),
        *_field_line('Less creditors & accruals:'),
        *_field_line('Less other borrowing / liabilities:'),
        *_field_line('Net capital employed:'),
        *_field_line('Number of employees:'),
        Paragraph('<font color="#b91c1c">* Above not applicable to start-up business</font>', st['small']),
    ]
    return flow


def _page5_sections_d_e() -> list:
    st = _styles()
    flow = [
        Spacer(1, 0.35 * inch),
        Paragraph('SECTION D — BANK ACCOUNT DETAILS FOR RECEIPT OF LOAN ADVANCE', st['section']),
        *_field_line('Bank name:'),
        *_field_line('Bank address:'),
        *_field_line('SWIFT code:'),
        *_field_line('Sort code:'),
        *_field_line('Account number:'),
        *_field_line('Account name:'),
        Spacer(1, 0.1 * inch),
        Paragraph('SECTION E — GENERAL', st['section']),
        *_field_line('1. Number of existing full-time equivalent employees:'),
        *_field_line('2. Over the period of the loan, how many full-time equivalent jobs will be created:'),
        Paragraph('3. Has an owner, partner or director of the business:', st['label']),
        Spacer(1, 0.02 * inch),
    ]
    questions = [
        'a) Been convicted of fraud or any other offence involving dishonesty?',
        'b) Been adjudged bankrupt or entered a personal voluntary creditors arrangement?',
        'c) Been a director (or substantial shareholder) of a company in liquidation or receivership, '
        'or subject to a creditors voluntary arrangement, or for which an administrator has been appointed?',
        'd) Been disqualified from the board of directors of a company?',
        'e) Had judgment entered against you in court?',
    ]
    for q in questions:
        flow.append(_checkbox_row(q))
    return flow


def _page6_declaration() -> list:
    st = _styles()
    flow = [
        Spacer(1, 0.35 * inch),
        Paragraph('DECLARATION', st['section']),
        Paragraph(
            'We may take up such references and make such enquiries about your company as we consider '
            'necessary, and we may use credit scoring and may search the files of credit reference agencies. '
            'The fact a search has been made will be recorded by each credit reference agency used and the '
            'data supplied will be available to other lenders and others authorised to search the credit '
            "reference agencies' files, for purposes such as credit assessment of your company and "
            'occasionally for debtor tracing and fraud prevention. If your application for finance is accepted '
            'then details about your company and the conduct of your account may be passed to credit reference '
            'agencies and these details will be used for similar purposes.',
            st['body'],
        ),
        Paragraph(
            'We may also disclose information about your company and the conduct of your account to credit '
            'industry fraud avoidance networks and to tracing and debt collection agencies and our solicitors.',
            st['body'],
        ),
        Paragraph(
            '<b>Data protection:</b> Your company information will be treated as confidential and will only be '
            'disclosed (a) at your request, (b) to our agents in connection with running your account, '
            '(c) in the public interest, (d) to prevent fraud or legal compulsion, or (e) when taking up '
            'references. Applicable data protection law gives you a right to a copy of your company records '
            'held on our files on payment of a fee.',
            st['body'],
        ),
        Spacer(1, 0.12 * inch),
    ]
    for n in (1, 2, 3):
        flow.extend(_signature_block(n))
    return flow


def _admin_table(headers: list[str], rows: list[list[str]]) -> Table:
    st = _styles()
    header_style = ParagraphStyle(
        'th',
        parent=st['table_cell'],
        fontName='Helvetica-Bold',
        textColor=WHITE,
    )

    def cell(text: str, header: bool = False) -> Paragraph | str:
        if not text:
            return ''
        style = header_style if header else st['table_cell']
        return Paragraph(escape(text), style)

    data = [[cell(h, header=True) for h in headers]]
    for row in rows:
        data.append([cell(c) if c else '' for c in row])

    col_w = CONTENT_WIDTH / len(headers)
    tbl = Table(data, colWidths=[col_w * inch] * len(headers))
    tbl.setStyle(
        TableStyle(
            [
                ('BACKGROUND', (0, 0), (-1, 0), PRIMARY_DARK),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 8),
                ('GRID', (0, 0), (-1, -1), 0.5, LINE_GREY),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('LEFTPADDING', (0, 0), (-1, -1), 6),
                ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ]
        )
    )
    return tbl


def _page7_official_use() -> list:
    st = _styles()
    blank = ''
    return [
        Spacer(1, 0.35 * inch),
        Paragraph('OFFICIAL USE ONLY', st['section']),
        _admin_table(
            ['Document', 'Date received', 'Checked by'],
            [
                ['Completed application form', blank, blank],
                ['Business plan with cash-flow forecast, credit report', blank, blank],
                ['ID certificate (passport / driving licence copy)', blank, blank],
                ['2 forms of address evidence', blank, blank],
            ],
        ),
        Spacer(1, 0.14 * inch),
        Paragraph('APPLICATION CHECKED AND COMPLETE', st['section']),
        _admin_table(['Name', 'Signature', 'Date'], [['', '', '']]),
        Spacer(1, 0.14 * inch),
        Paragraph('LOAN ADMINISTRATION', st['section']),
        _admin_table(
            ['Task', 'Date', 'Loan officer'],
            [
                ['Pre-contract issued', blank, blank],
                ['Loan agreement signed', blank, blank],
                ['Direct debit signed', blank, blank],
                ['Loan released', blank, blank],
            ],
        ),
    ]


def generate_loan_application_pdf(prefill: LoanFormPrefill | None = None) -> bytes:
    prefill = prefill or LoanFormPrefill()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.55 * inch,
        leftMargin=0.55 * inch,
        topMargin=1.15 * inch,
        bottomMargin=0.55 * inch,
    )

    pages = [
        _page1_cover(),
        _page2_section_a(prefill),
        _page3_section_b(prefill),
        _page4_section_c(),
        _page5_sections_d_e(),
        _page6_declaration(),
        _page7_official_use(),
    ]

    flow = []
    for i, page_flow in enumerate(pages):
        if i > 0:
            flow.append(PageBreak())
        flow.extend(page_flow)

    doc.build(flow, onFirstPage=_draw_header_footer, onLaterPages=_draw_header_footer)
    return buffer.getvalue()


def email_loan_application_pdf(to_email: str, prefill: LoanFormPrefill | None = None) -> None:
    from apps.notifications.email_assets import send_branded_email
    from apps.notifications.email_layout import get_from_email_for_event, render_custom_email

    pdf_bytes = generate_loan_application_pdf(prefill)
    subject = 'Your SafaPay Bank project loan funding application (PDF attached)'
    greeting = prefill.applicant_name if prefill and prefill.applicant_name else 'customer'
    inner_body = '\n'.join(
        [
            f'Dear {greeting},',
            '',
            'Please find attached the SafaPay Bank project loan funding application form.',
            'Complete every section, gather the supporting documents listed on page 1, and return '
            'the signed form to us when you are ready to proceed.',
            '',
            'If you did not expect this email, please contact us immediately.',
        ]
    )
    _, text_body, html_body = render_custom_email(subject=subject, text_body=inner_body)
    fname = 'SafaPay_Project_Loan_Application.pdf'

    send_branded_email(
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        from_email=get_from_email_for_event('loan_application_form', {}),
        recipient_list=[to_email],
        fail_silently=False,
        attachments=[(fname, pdf_bytes, 'application/pdf')],
    )
