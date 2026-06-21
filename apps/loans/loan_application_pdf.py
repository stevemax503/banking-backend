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
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus.flowables import Flowable

from apps.users.models import CustomUser

PRIMARY_DARK = colors.HexColor('#152A1E')
PRIMARY = colors.HexColor('#1E3A2A')
ACCENT = colors.HexColor('#C8F000')
BLACK = colors.black
GREY = colors.HexColor('#4b5563')
LINE_GREY = colors.HexColor('#9ca3af')
WHITE = colors.white
RED = colors.HexColor('#b91c1c')

BANK_NAME = 'SafaPay Bank'
BANK_SHORT = 'SafaPay'
BANK_TAGLINE = 'Purity, clarity, and trust'
CONTENT_WIDTH = 6.9  # inches
TICK_BOX_PT = 11
# Header layout (fixed heights — keeps rule clear of contact text)
HEADER_BAND_H = 0.46 * inch
HEADER_INFO_H = 0.44 * inch
HEADER_RULE_GAP = 10
CONTENT_BELOW_RULE = 14


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


def _header_contact() -> tuple[str, str, str, str]:
    support = getattr(settings, 'SUPPORT_EMAIL', 'support@safapaygroup.com')
    phone = getattr(settings, 'STATEMENT_SUPPORT_PHONE', '') or '1-800-SAFA-PAY'
    return BANK_NAME, support, phone, BANK_TAGLINE


def _header_content_top() -> float:
    """Distance from page top to the start of the main body frame."""
    return HEADER_BAND_H + HEADER_INFO_H + HEADER_RULE_GAP + CONTENT_BELOW_RULE


def _draw_header_band(canvas, w: float, h: float, left_m: float, right_m: float) -> float:
    """Dark branded band at top; returns y of band bottom."""
    band_bottom = h - HEADER_BAND_H

    canvas.setFillColor(PRIMARY_DARK)
    canvas.rect(0, band_bottom, w, HEADER_BAND_H, fill=1, stroke=0)
    canvas.setFillColor(ACCENT)
    canvas.rect(0, band_bottom, w, 2.5, fill=1, stroke=0)

    # Growth mark (accent bars)
    mark_x = left_m
    mark_base = band_bottom + HEADER_BAND_H * 0.22
    mark_h = HEADER_BAND_H * 0.56
    canvas.setFillColor(ACCENT)
    canvas.rect(mark_x, mark_base, 3.5, mark_h * 0.45, fill=1, stroke=0)
    canvas.rect(mark_x + 5, mark_base + mark_h * 0.2, 3.5, mark_h * 0.8, fill=1, stroke=0)
    canvas.rect(mark_x + 10, mark_base + mark_h * 0.05, 3.5, mark_h * 0.6, fill=1, stroke=0)

    text_x = mark_x + 20
    canvas.setFillColor(WHITE)
    canvas.setFont('Helvetica-Bold', 15)
    canvas.drawString(text_x, band_bottom + HEADER_BAND_H * 0.48, BANK_SHORT)
    canvas.setFont('Helvetica-Oblique', 7)
    canvas.setFillColor(ACCENT)
    canvas.drawString(text_x, band_bottom + HEADER_BAND_H * 0.22, BANK_TAGLINE)

    canvas.setFillColor(WHITE)
    canvas.setFont('Helvetica-Bold', 7.5)
    canvas.drawRightString(right_m, band_bottom + HEADER_BAND_H * 0.52, 'Project Loan Application')
    canvas.setFont('Helvetica', 7)
    canvas.setFillColor(colors.HexColor('#c8d4cc'))
    canvas.drawRightString(right_m, band_bottom + HEADER_BAND_H * 0.28, BANK_NAME)

    return band_bottom


def _draw_header_footer(canvas, _doc):
    canvas.saveState()
    w, h = letter
    left_m = 0.55 * inch
    right_m = w - 0.55 * inch
    mid_x = w * 0.52

    band_bottom = _draw_header_band(canvas, w, h, left_m, right_m)

    bank, support, phone, tagline = _header_contact()
    info_top = band_bottom - 8

    canvas.setFillColor(PRIMARY_DARK)
    canvas.setFont('Helvetica-Bold', 7.5)
    canvas.drawString(left_m, info_top, 'Head office')
    canvas.drawString(mid_x, info_top, 'Customer support')

    canvas.setFont('Helvetica', 7.5)
    canvas.setFillColor(GREY)
    y_left = info_top - 11
    for line in (bank, 'Global operations · Digital-first banking', support):
        canvas.drawString(left_m, y_left, line)
        y_left -= 9

    y_right = info_top - 11
    for line in (phone, tagline):
        canvas.drawString(mid_x, y_right, line)
        y_right -= 9

    rule_y = min(y_left, y_right) - HEADER_RULE_GAP
    canvas.setStrokeColor(ACCENT)
    canvas.setLineWidth(1.25)
    canvas.line(left_m, rule_y, right_m, rule_y)
    canvas.setStrokeColor(colors.HexColor('#e5ebe8'))
    canvas.setLineWidth(0.5)
    canvas.line(left_m, rule_y - 3, right_m, rule_y - 3)

    foot_h = 0.30 * inch
    canvas.setFillColor(PRIMARY_DARK)
    canvas.rect(0, 0, w, foot_h, fill=1, stroke=0)
    canvas.setFillColor(ACCENT)
    canvas.rect(0, foot_h, w, 2.5, fill=1, stroke=0)

    canvas.setFillColor(WHITE)
    canvas.setFont('Helvetica-Bold', 7.5)
    canvas.drawString(left_m, 0.11 * inch, BANK_SHORT)
    canvas.setFont('Helvetica', 7)
    canvas.drawCentredString(w / 2, 0.11 * inch, getattr(settings, 'SUPPORT_EMAIL', 'support@safapaygroup.com'))
    canvas.setFont('Helvetica-Bold', 7.5)
    canvas.drawRightString(right_m, 0.11 * inch, f'Page {canvas.getPageNumber()}')

    canvas.restoreState()


_STYLES: dict | None = None


def _styles() -> dict:
    global _STYLES
    if _STYLES is not None:
        return _STYLES

    base = getSampleStyleSheet()
    _STYLES = {
        'title': ParagraphStyle(
            'lf_title',
            parent=base['Normal'],
            fontSize=16,
            leading=21,
            fontName='Helvetica-Bold',
            textColor=BLACK,
            alignment=1,
            spaceAfter=12,
        ),
        'section': ParagraphStyle(
            'lf_section',
            parent=base['Normal'],
            fontSize=11.5,
            leading=15,
            fontName='Helvetica-Bold',
            textColor=PRIMARY_DARK,
            spaceBefore=3,
            spaceAfter=10,
        ),
        'body': ParagraphStyle(
            'lf_body',
            parent=base['Normal'],
            fontSize=10,
            leading=14,
            fontName='Helvetica',
            textColor=BLACK,
            spaceAfter=6,
        ),
        'label': ParagraphStyle(
            'lf_label',
            parent=base['Normal'],
            fontSize=10,
            leading=14,
            fontName='Helvetica-Bold',
            textColor=BLACK,
        ),
        'label_wrap': ParagraphStyle(
            'lf_label_wrap',
            parent=base['Normal'],
            fontSize=10,
            leading=14,
            fontName='Helvetica-Bold',
            textColor=BLACK,
        ),
        'value': ParagraphStyle(
            'lf_value',
            parent=base['Normal'],
            fontSize=10,
            leading=14,
            fontName='Helvetica',
            textColor=BLACK,
        ),
        'warn': ParagraphStyle(
            'lf_warn',
            parent=base['Normal'],
            fontSize=10.5,
            leading=14,
            fontName='Helvetica-Bold',
            textColor=RED,
            spaceAfter=8,
        ),
        'small': ParagraphStyle(
            'lf_small',
            parent=base['Normal'],
            fontSize=9,
            leading=12,
            fontName='Helvetica',
            textColor=GREY,
            spaceAfter=5,
        ),
        'item': ParagraphStyle(
            'lf_item',
            parent=base['Normal'],
            fontSize=10,
            leading=14,
            fontName='Helvetica',
            textColor=BLACK,
            leftIndent=8,
            spaceAfter=5,
        ),
        'table_cell': ParagraphStyle(
            'lf_table_cell',
            parent=base['Normal'],
            fontSize=9.5,
            leading=13,
            fontName='Helvetica',
            textColor=BLACK,
        ),
        'checkbox_q': ParagraphStyle(
            'lf_checkbox_q',
            parent=base['Normal'],
            fontSize=9.5,
            leading=13,
            fontName='Helvetica',
            textColor=BLACK,
        ),
        'checkbox_opt': ParagraphStyle(
            'lf_checkbox_opt',
            parent=base['Normal'],
            fontSize=9.5,
            leading=13,
            fontName='Helvetica',
            textColor=BLACK,
            alignment=2,
        ),
    }
    return _STYLES


def _p(text: str, style_key: str = 'body') -> Paragraph:
    return Paragraph(escape(text), _styles()[style_key])


def _table_pad() -> TableStyle:
    return TableStyle(
        [
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ]
    )


class TickBox(Flowable):
    """Empty white square with black border — tick/cross visible when filled in."""

    def __init__(self, size: float = TICK_BOX_PT):
        super().__init__()
        self.box_size = size
        self.width = size
        self.height = size

    def draw(self):
        canv = self.canv
        canv.saveState()
        canv.setStrokeColor(BLACK)
        canv.setFillColor(WHITE)
        canv.setLineWidth(0.8)
        canv.rect(0, 0, self.box_size, self.box_size, fill=1, stroke=1)
        canv.restoreState()


def _yn_pair(label: str) -> Table:
    """Checkbox + Yes/No label with clear gap between box and text."""
    st = _styles()
    yn = ParagraphStyle('lf_yn_lbl', parent=st['body'], fontSize=9.5, leading=12, spaceAfter=0)
    box_w = TICK_BOX_PT + 2
    label_w = 36
    tbl = Table(
        [[TickBox(), Paragraph(escape(label), yn)]],
        colWidths=[box_w, label_w],
    )
    tbl.setStyle(
        TableStyle(
            [
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('LEFTPADDING', (0, 0), (-1, -1), 0),
                ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                ('LEFTPADDING', (1, 0), (1, 0), 5),
                ('TOPPADDING', (0, 0), (-1, -1), 0),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
            ]
        )
    )
    return tbl


def _business_type_row() -> Table:
    """Section A — four tickable boxes in one row."""
    st = _styles()
    opt = ParagraphStyle('lf_cb_lbl', parent=st['body'], spaceAfter=0, leading=12)
    box_col = (TICK_BOX_PT + 5) / 72.0
    label_col = (CONTENT_WIDTH - 4 * box_col) / 4
    return Table(
        [
            [
                TickBox(),
                Paragraph('a) Sole trader', opt),
                TickBox(),
                Paragraph('b) Limited company', opt),
                TickBox(),
                Paragraph('c) Partnership', opt),
                TickBox(),
                Paragraph('d) Other', opt),
            ]
        ],
        colWidths=[
            box_col * inch,
            label_col * inch,
            box_col * inch,
            label_col * inch,
            box_col * inch,
            label_col * inch,
            box_col * inch,
            label_col * inch,
        ],
        style=TableStyle(
            [
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('LEFTPADDING', (0, 0), (-1, -1), 0),
                ('RIGHTPADDING', (0, 0), (-1, -1), 2),
                ('TOPPADDING', (0, 0), (-1, -1), 3),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ]
        ),
    )


def _label_column_width(label: str, indent: float = 0) -> float:
    """Label column width in inches — line starts immediately after label text."""
    st = _styles()
    font_name = st['label'].fontName
    font_size = st['label'].fontSize
    text_w_pt = stringWidth(label, font_name, font_size)
    total_pt = indent + text_w_pt + 5  # 5pt gap before line
    label_in = total_pt / 72.0
    return min(label_in, CONTENT_WIDTH - 0.6)


def _labeled_line(
    label: str,
    value: str = '',
    *,
    indent: float = 0,
) -> list:
    """Label then underline running to the right margin (no wide gap)."""
    st = _styles()
    label_w = _label_column_width(label, indent)
    line_w = CONTENT_WIDTH - label_w
    label_style = ParagraphStyle(
        'lf_lbl_ind',
        parent=st['label_wrap'],
        leftIndent=indent,
    )
    label_cell = Paragraph(escape(label), label_style)

    if value:
        row = Table(
            [[label_cell, Paragraph(escape(value), st['value'])]],
            colWidths=[label_w * inch, line_w * inch],
        )
        row.setStyle(
            TableStyle(
                [
                    *_table_pad().getCommands(),
                    ('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
                    ('RIGHTPADDING', (0, 0), (0, 0), 0),
                    ('LEFTPADDING', (1, 0), (1, 0), 0),
                ]
            )
        )
    else:
        row = Table(
            [[label_cell, '']],
            colWidths=[label_w * inch, line_w * inch],
        )
        row.setStyle(
            TableStyle(
                [
                    *_table_pad().getCommands(),
                    ('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
                    ('LINEBELOW', (1, 0), (1, 0), 0.5, LINE_GREY),
                    ('RIGHTPADDING', (0, 0), (0, 0), 0),
                    ('LEFTPADDING', (1, 0), (1, 0), 0),
                ]
            )
        )
    return [row, Spacer(1, 0.045 * inch)]


def _multiline_box(label: str, value: str = '', rows: int = 3) -> list:
    st = _styles()
    flow: list = [_p(label, 'label'), Spacer(1, 0.03 * inch)]
    if value:
        flow.append(Paragraph(escape(value).replace('\n', '<br/>'), st['value']))
    else:
        box = Table(
            [[''] for _ in range(rows)],
            colWidths=[CONTENT_WIDTH * inch],
            rowHeights=[0.26 * inch] * rows,
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
    flow.append(Spacer(1, 0.08 * inch))
    return flow


def _checkbox_row(label: str) -> Table:
    st = _styles()
    yn_col = 1.05 * inch
    return Table(
        [
            [
                Paragraph(escape(label), st['checkbox_q']),
                _yn_pair('Yes'),
                _yn_pair('No'),
            ]
        ],
        colWidths=[CONTENT_WIDTH * inch - 2 * yn_col, yn_col, yn_col],
        style=TableStyle(
            [
                *_table_pad().getCommands(),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('ALIGN', (1, 0), (2, 0), 'CENTER'),
                ('LEFTPADDING', (1, 0), (2, 0), 4),
            ]
        ),
    )


def _signature_block(n: int) -> list:
    st = _styles()
    sig_line = Table([['']], colWidths=[CONTENT_WIDTH * inch], rowHeights=[0.24 * inch])
    sig_line.setStyle(TableStyle([('LINEBELOW', (0, 0), (-1, -1), 0.5, LINE_GREY)]))

    detail = Table(
        [
            [
                _p('Name:', 'label'),
                '',
                _p('Position:', 'label'),
                '',
                _p('Date:', 'label'),
                '',
            ]
        ],
        colWidths=[0.5 * inch, 2.35 * inch, 0.65 * inch, 2.35 * inch, 0.38 * inch, 0.67 * inch],
    )
    detail.setStyle(
        TableStyle(
            [
                *_table_pad().getCommands(),
                ('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
                ('LINEBELOW', (1, 0), (1, 0), 0.5, LINE_GREY),
                ('LINEBELOW', (3, 0), (3, 0), 0.5, LINE_GREY),
                ('LINEBELOW', (5, 0), (5, 0), 0.5, LINE_GREY),
            ]
        )
    )

    return [
        _p(f'{n}. Signature(s):', 'label'),
        sig_line,
        Spacer(1, 0.06 * inch),
        detail,
        Spacer(1, 0.16 * inch),
    ]


def _bullet_list(items: Iterable[str]) -> list:
    return [_p(f'• {item}', 'item') for item in items]


def _page1_cover() -> list:
    st = _styles()
    flow = [
        Spacer(1, 0.12 * inch),
        _p('PROJECT LOAN FUNDING APPLICATION', 'title'),
        Spacer(1, 0.12 * inch),
        _p('Please read the following carefully', 'warn'),
        Paragraph(
            '<b>IT IS IMPORTANT THAT YOU PROVIDE THE FOLLOWING INFORMATION AND DOCUMENTS FOR US.</b>',
            st['body'],
        ),
        _p(
            'If you submit without a fully completed application form and all supporting documents '
            'we will not be able to help you.',
        ),
        _p(
            'This information is required to help us deal with your application speedily and efficiently. '
            'If it is apparent that information has been withheld or appears to be false, your application '
            'will not be considered.',
        ),
        Spacer(1, 0.06 * inch),
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
        _p(f'Questions? Contact {getattr(settings, "SUPPORT_EMAIL", "support@safapaygroup.com")}.', 'small')
    )
    return flow


def _page2_section_a(prefill: LoanFormPrefill) -> list:
    st = _styles()
    flow = [
        Spacer(1, 0.12 * inch),
        _p('SECTION A — APPLICATION DETAILS', 'section'),
        *_labeled_line('1. Applicant name(s):', prefill.applicant_name),
        *_labeled_line('2. Business name:', prefill.business_name or prefill.applicant_name),
        *_multiline_box('3. Business address:', prefill.address, 2),
        *_labeled_line('4. Business telephone & fax number:', prefill.phone),
        *_labeled_line('5. E-mail address:', prefill.email),
        _p('6. Business type (please tick as appropriate):', 'label'),
        _business_type_row(),
        Spacer(1, 0.04 * inch),
        *_labeled_line('7. Company registered number (if applicable):'),
        *_labeled_line('8. Date business was established:'),
        *_labeled_line('9. Accounting year end:'),
        *_multiline_box('10. Brief description of what business does:', rows=3),
        Paragraph('<i>If the business is a partnership, please complete section 11.</i>', st['small']),
        *_labeled_line('11a. Partner name:'),
        *_labeled_line('Equity held:', indent=14),
        *_labeled_line('11b. Partner name:'),
        *_labeled_line('Equity held:', indent=14),
        *_labeled_line('Others:'),
    ]
    return flow


def _page3_section_b(prefill: LoanFormPrefill) -> list:
    flow = [
        Spacer(1, 0.12 * inch),
        _p('SECTION B — GENERAL INFORMATION ABOUT LOAN REQUESTED', 'section'),
        *_multiline_box(
            '1. Purpose of loan — why are you applying (brief description of the project / purpose)?',
            prefill.loan_purpose,
            4,
        ),
        *_labeled_line('2. Total cost of project (to match A below):'),
        *_labeled_line('3. Loan amount requested (to match B below):', prefill.loan_amount),
        *_labeled_line('4. Over what period? (duration of loan requested):', prefill.loan_term),
        *_labeled_line('5. Do you wish to repay by monthly or quarterly instalment?'),
        *_labeled_line('6. Do you require a capital repayment holiday? Yes / No'),
        _p('7. Detailed project costs', 'label'),
        *_labeled_line('$'),
        *_labeled_line('$'),
        _p('Equipment / machinery · working capital · other items (please specify)', 'small'),
        *_labeled_line('Total cost of project (A) less loan from SafaPay Bank (B):'),
        *_labeled_line('Total of non-SafaPay Bank funding (C): $'),
        *_labeled_line('8. What are the sources of non-SafaPay Bank funding?'),
        *_labeled_line('Your own financial contribution:', indent=12),
        *_labeled_line('Bank loan / overdraft:', indent=12),
        *_labeled_line('HP / Leasing:', indent=12),
        *_labeled_line('Grants:', indent=12),
    ]
    return flow


def _page4_section_c() -> list:
    st = _styles()
    flow = [
        Spacer(1, 0.12 * inch),
        _p('SECTION C — FINANCIAL RESULTS', 'section'),
        _p('Historic accounts — last 2 months *', 'label'),
        *_labeled_line('Month ending:'),
        *_labeled_line('Month ending:'),
        *_labeled_line('Sales:'),
        *_labeled_line('Gross profits:'),
        *_labeled_line('Overheads:'),
        *_labeled_line('Net profit before interest, tax & drawings (see 3 below):'),
        Spacer(1, 0.04 * inch),
        *_labeled_line('Fixed assets:'),
        *_labeled_line('Plus stock:'),
        *_labeled_line('Plus debtors & payments:'),
        *_labeled_line('Plus cash:'),
        *_labeled_line('Total assets:'),
        *_labeled_line('Less creditors & accruals:'),
        *_labeled_line('Less other borrowing / liabilities:'),
        *_labeled_line('Net capital employed:'),
        *_labeled_line('Number of employees:'),
        Paragraph('<font color="#b91c1c">* Above not applicable to start-up business</font>', st['small']),
    ]
    return flow


def _page5_sections_d_e() -> list:
    flow = [
        Spacer(1, 0.12 * inch),
        _p('SECTION D — BANK ACCOUNT DETAILS FOR RECEIPT OF LOAN ADVANCE', 'section'),
        *_labeled_line('Bank name:'),
        *_labeled_line('Bank address:'),
        *_labeled_line('SWIFT code:'),
        *_labeled_line('Sort code:'),
        *_labeled_line('Account number:'),
        *_labeled_line('Account name:'),
        Spacer(1, 0.1 * inch),
        _p('SECTION E — GENERAL', 'section'),
        *_labeled_line('1. Number of existing full-time equivalent employees:'),
        *_labeled_line(
            '2. Over the period of the loan, how many full-time equivalent jobs will be created:',
        ),
        _p('3. Has an owner, partner or director of the business:', 'label'),
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
        Spacer(1, 0.12 * inch),
        _p('DECLARATION', 'section'),
        _p(
            'We may take up such references and make such enquiries about your company as we consider '
            'necessary, and we may use credit scoring and may search the files of credit reference agencies. '
            'The fact a search has been made will be recorded by each credit reference agency used and the '
            'data supplied will be available to other lenders and others authorised to search the credit '
            "reference agencies' files, for purposes such as credit assessment of your company and "
            'occasionally for debtor tracing and fraud prevention. If your application for finance is accepted '
            'then details about your company and the conduct of your account may be passed to credit reference '
            'agencies and these details will be used for similar purposes.',
        ),
        _p(
            'We may also disclose information about your company and the conduct of your account to credit '
            'industry fraud avoidance networks and to tracing and debt collection agencies and our solicitors.',
        ),
        Paragraph(
            '<b>Data protection:</b> Your company information will be treated as confidential and will only be '
            'disclosed (a) at your request, (b) to our agents in connection with running your account, '
            '(c) in the public interest, (d) to prevent fraud or legal compulsion, or (e) when taking up '
            'references. Applicable data protection law gives you a right to a copy of your company records '
            'held on our files on payment of a fee.',
            st['body'],
        ),
        Spacer(1, 0.1 * inch),
    ]
    for n in (1, 2, 3):
        flow.extend(_signature_block(n))
    return flow


def _admin_table(headers: list[str], rows: list[list[str]]) -> Table:
    st = _styles()
    header_style = ParagraphStyle(
        'lf_th',
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
                ('GRID', (0, 0), (-1, -1), 0.5, LINE_GREY),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                ('TOPPADDING', (0, 0), (-1, -1), 8),
                ('LEFTPADDING', (0, 0), (-1, -1), 8),
                ('RIGHTPADDING', (0, 0), (-1, -1), 8),
            ]
        )
    )
    return tbl


def _page7_official_use() -> list:
    blank = ''
    return [
        Spacer(1, 0.12 * inch),
        _p('OFFICIAL USE ONLY', 'section'),
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
        _p('APPLICATION CHECKED AND COMPLETE', 'section'),
        _admin_table(['Name', 'Signature', 'Date'], [['', '', '']]),
        Spacer(1, 0.14 * inch),
        _p('LOAN ADMINISTRATION', 'section'),
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
        topMargin=_header_content_top(),
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
