"""PDF account statements (ReportLab) and email delivery."""
from __future__ import annotations

import io
from decimal import Decimal
from datetime import date
from xml.sax.saxutils import escape

from django.conf import settings
from django.core.files.base import ContentFile
from apps.notifications.email_assets import send_branded_email
from apps.notifications.email_layout import get_from_email_for_event, render_custom_email
from django.db.models import Q
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from apps.accounts.models import Account
from apps.transactions.models import Transaction

from .models import Statement

# Statement chrome (Parallex-inspired layout; generic branding via settings)
NAVY = colors.HexColor('#0f2744')
GOLD = colors.HexColor('#c6a035')
GREY_HEADER = colors.HexColor('#d8d8d8')
GREY_ALT = colors.HexColor('#f5f4f0')
WHITE = colors.white
BLACK = colors.black


def _statement_branding():
    phone = getattr(settings, 'STATEMENT_SUPPORT_PHONE', '') or ''
    mail = getattr(settings, 'STATEMENT_SUPPORT_EMAIL', '') or getattr(settings, 'DEFAULT_FROM_EMAIL', '')
    return {
        'bank_name': getattr(settings, 'STATEMENT_BANK_NAME', 'bankApp'),
        'support_phone': phone,
        'support_email': mail,
        'address': getattr(settings, 'STATEMENT_BANK_ADDRESS', '') or '',
    }


def _fmt_number(val: Decimal) -> str:
    """Statement amounts: no currency symbol (customer-facing USD presentation)."""
    return f'{val:,.2f}'


def _statement_display_currency() -> str:
    return getattr(settings, 'STATEMENT_DISPLAY_CURRENCY', 'USD') or 'USD'


def _transactions_for_statement(account: Account, period_start: date, period_end: date):
    return list(
        Transaction.objects.filter(
            Q(from_account=account) | Q(to_account=account),
            created_at__date__gte=period_start,
            created_at__date__lte=period_end,
            status=Transaction.Status.COMPLETED,
        ).order_by('created_at', 'id')
    )


def _opening_balance_before_period(account: Account, txs: list[Transaction]) -> Decimal:
    """Balance immediately before the first transaction in the statement window."""
    aid = str(account.id)
    cur = account.balance
    for tx in reversed(txs):
        if tx.to_account_id and str(tx.to_account_id) == aid:
            cur -= tx.amount
        elif tx.from_account_id and str(tx.from_account_id) == aid:
            cur += tx.amount
    return cur


def generate_statement_pdf(statement: Statement) -> bytes:
    brand = _statement_branding()
    account = statement.account
    owner = account.owner
    display_ccy = _statement_display_currency()
    txs = _transactions_for_statement(account, statement.period_start, statement.period_end)

    opening = _opening_balance_before_period(account, txs)
    total_credit = Decimal('0')
    total_debit = Decimal('0')
    running = opening

    styles = getSampleStyleSheet()
    narr_style = ParagraphStyle(
        'nar',
        parent=styles['Normal'],
        fontSize=6.5,
        leading=8,
        textColor=BLACK,
        fontName='Helvetica',
    )

    rows = [['Trans Date', 'Value Date', 'Debit', 'Credit', 'Balance', 'Narration']]
    aid = str(account.id)
    for tx in txs:
        vd = tx.completed_at.date() if tx.completed_at else tx.created_at.date()
        td = tx.created_at.date()
        is_credit = bool(tx.to_account_id and str(tx.to_account_id) == aid)
        debit_amt = Decimal('0') if is_credit else tx.amount
        credit_amt = tx.amount if is_credit else Decimal('0')
        if is_credit:
            total_credit += tx.amount
        else:
            total_debit += tx.amount
        running = running + (tx.amount if is_credit else -tx.amount)
        narr = (tx.description or tx.transaction_type.replace('_', ' ')).upper()
        if len(narr) > 200:
            narr = narr[:197] + '...'
        narr_para = Paragraph(escape(narr), narr_style)
        rows.append(
            [
                td.strftime('%d/%m/%Y'),
                vd.strftime('%d/%m/%Y'),
                _fmt_number(debit_amt) if debit_amt else '0.00',
                _fmt_number(credit_amt) if credit_amt else '0.00',
                _fmt_number(running),
                narr_para,
            ]
        )
    if len(rows) == 1:
        rows.append(
            [
                '—',
                '—',
                '0.00',
                '0.00',
                _fmt_number(opening),
                Paragraph('NO TRANSACTIONS IN THIS PERIOD', narr_style),
            ]
        )

    closing = account.balance
    available = account.available_balance

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=0.45 * inch,
        leftMargin=0.45 * inch,
        topMargin=0.95 * inch,
        bottomMargin=1.05 * inch,
    )

    def _draw_frame(canvas, _doc):
        canvas.saveState()
        w, h = letter
        left_m = 0.45 * inch
        right_m = w - 0.45 * inch
        top_bar_h = 0.34 * inch
        bar_bottom = h - 0.62 * inch
        canvas.setStrokeColor(GOLD)
        canvas.setLineWidth(2)
        canvas.line(left_m, bar_bottom + top_bar_h + 0.08 * inch, right_m, bar_bottom + top_bar_h + 0.08 * inch)
        canvas.setFillColor(NAVY)
        canvas.rect(left_m, bar_bottom, right_m - left_m, top_bar_h, fill=1, stroke=0)
        canvas.setFillColor(GOLD)
        canvas.setFont('Helvetica-Bold', 11)
        canvas.drawRightString(right_m - 0.12 * inch, bar_bottom + 0.09 * inch, brand['bank_name'])

        foot_y = 0.38 * inch
        canvas.setStrokeColor(GOLD)
        canvas.setLineWidth(1)
        canvas.line(left_m, foot_y + 0.52 * inch, right_m, foot_y + 0.52 * inch)
        canvas.setFillColor(NAVY)
        canvas.setFont('Helvetica', 8)
        if brand['support_phone']:
            canvas.drawString(left_m, foot_y + 0.28 * inch, brand['support_phone'])
        if brand['support_email']:
            canvas.drawCentredString(w / 2, foot_y + 0.28 * inch, brand['support_email'])
        if brand['address']:
            addr = brand['address']
            if len(addr) > 70:
                addr = addr[:67] + '...'
            canvas.drawRightString(right_m, foot_y + 0.1 * inch, addr)
        canvas.setFont('Helvetica-Bold', 8)
        canvas.drawRightString(right_m, foot_y + 0.34 * inch, f'Page {canvas.getPageNumber()}')
        canvas.restoreState()

    name_style = ParagraphStyle('n', parent=styles['Normal'], fontSize=16, leading=20, textColor=BLACK, fontName='Helvetica-Bold')
    sub_style = ParagraphStyle('s', parent=styles['Normal'], fontSize=9, leading=12, textColor=colors.HexColor('#4b5563'))
    small_white = ParagraphStyle('sw', parent=styles['Normal'], fontSize=8, leading=11, textColor=WHITE)

    flow = []

    flow.append(Spacer(1, 0.02 * inch))
    flow.append(Paragraph(owner.full_name or owner.email, name_style))
    flow.append(
        Paragraph(
            f"This is your account statement: {statement.period_start.strftime('%d-%b-%Y')} to "
            f"{statement.period_end.strftime('%d-%b-%Y')}",
            sub_style,
        )
    )
    flow.append(Spacer(1, 0.14 * inch))

    left_info = Table(
        [
            [Paragraph('Account number', small_white), Paragraph(account.account_number, small_white)],
            [
                Paragraph('Account type', small_white),
                Paragraph(f'{account.get_account_type_display().upper()} ({display_ccy})', small_white),
            ],
            [Paragraph('Currency', small_white), Paragraph(display_ccy, small_white)],
        ],
        colWidths=[1.15 * inch, 1.85 * inch],
    )
    left_info.setStyle(
        TableStyle(
            [
                ('BACKGROUND', (0, 0), (-1, -1), NAVY),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('LEFTPADDING', (0, 0), (-1, -1), 10),
                ('RIGHTPADDING', (0, 0), (-1, -1), 10),
            ]
        )
    )

    summary_right = [
        ['Account type', f'{account.get_account_type_display().upper()} ({display_ccy})'],
        ['Total credit', _fmt_number(total_credit)],
        ['Total debit', _fmt_number(total_debit)],
        ['Opening balance', _fmt_number(opening)],
        ['Closing balance', _fmt_number(closing)],
        ['Available balance', _fmt_number(available)],
    ]
    right_tbl = Table(summary_right, colWidths=[1.45 * inch, 2.05 * inch])
    right_tbl.setStyle(
        TableStyle(
            [
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 0), (-1, -1), 8),
                ('ROWBACKGROUNDS', (0, 0), (-1, -1), [colors.white, colors.HexColor('#efefef')]),
                ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#d1d5db')),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                ('TOPPADDING', (0, 0), (-1, -1), 5),
                ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ]
        )
    )

    summary_row = Table([[left_info, right_tbl]], colWidths=[3.2 * inch, 3.55 * inch])
    summary_row.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0)]))
    flow.append(summary_row)
    flow.append(Spacer(1, 0.16 * inch))

    col_widths = [0.72 * inch, 0.72 * inch, 0.78 * inch, 0.78 * inch, 0.92 * inch, 2.75 * inch]
    tx_table = Table(rows, colWidths=col_widths, repeatRows=1)
    tx_table.setStyle(
        TableStyle(
            [
                ('BACKGROUND', (0, 0), (-1, 0), GREY_HEADER),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 7),
                ('TEXTCOLOR', (0, 0), (-1, 0), BLACK),
                ('ALIGN', (0, 0), (1, -1), 'LEFT'),
                ('ALIGN', (2, 0), (4, -1), 'RIGHT'),
                ('ALIGN', (5, 0), (5, -1), 'LEFT'),
                ('FONTSIZE', (0, 1), (-1, -1), 7),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, GREY_ALT]),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('LEFTPADDING', (0, 0), (-1, -1), 4),
                ('RIGHTPADDING', (0, 0), (-1, -1), 4),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ]
        )
    )
    flow.append(tx_table)
    flow.append(Spacer(1, 0.12 * inch))
    flow.append(
        Paragraph(
            'This statement is for your records. Report any discrepancies within 30 days of the statement date.',
            sub_style,
        )
    )

    doc.build(flow, onFirstPage=_draw_frame, onLaterPages=_draw_frame)
    return buffer.getvalue()


def create_or_regenerate_statement(account_id: str, period_start: date, period_end: date) -> Statement:
    account = Account.objects.select_related('owner', 'currency').get(pk=account_id)
    statement, _ = Statement.objects.get_or_create(
        account=account,
        period_start=period_start,
        period_end=period_end,
    )
    statement = Statement.objects.select_related('account__owner', 'account__currency').get(pk=statement.pk)
    pdf_bytes = generate_statement_pdf(statement)
    filename = f'statement_{account.account_number}_{period_start}_{period_end}.pdf'
    statement.pdf_file.save(filename, ContentFile(pdf_bytes), save=True)
    return statement


def email_statement_pdf(statement: Statement, to_email: str, e_signed: bool = False) -> None:
    """Send the generated PDF to the customer. Requires SMTP to be configured."""
    if not statement.pdf_file:
        raise ValueError('Statement has no PDF file.')
    account = statement.account
    subject = 'Your account statement (PDF attached)'
    if e_signed:
        subject = 'Your account statement — e-signed copy (PDF attached)'

    body_lines = [
        f'Dear {account.owner.full_name or "customer"},',
        '',
        f'Please find attached your account statement for {account.account_number}',
        f'covering {statement.period_start.strftime("%d %b %Y")} to {statement.period_end.strftime("%d %b %Y")}.',
        '',
    ]
    if e_signed:
        body_lines.append('You requested an e-signed statement; this PDF is your official copy for that request.')
        body_lines.append('')
    body_lines.extend(
        [
            'If you did not request this email, please contact us immediately.',
        ]
    )
    inner_body = '\n'.join(body_lines)
    _, text_body, html_body = render_custom_email(subject=subject, text_body=inner_body)

    with statement.pdf_file.open('rb') as pdf_f:
        pdf_bytes = pdf_f.read()
    fname = statement.pdf_file.name.split('/')[-1] or 'statement.pdf'

    send_branded_email(
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        from_email=get_from_email_for_event('statement_ready', {}),
        recipient_list=[to_email],
        fail_silently=False,
        attachments=[(fname, pdf_bytes, 'application/pdf')],
    )
