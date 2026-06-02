"""Per-line compliance fees with independent OTP steps (international transfer & loan payout)."""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


class ComplianceFeeLine(models.Model):
    """Admin-defined named fee line (e.g. Tax, AML). Each line = separate charge + OTP."""

    class AppliesTo(models.TextChoices):
        INTERNATIONAL_TRANSFER = 'INTERNATIONAL_TRANSFER', 'International transfer'
        LOAN_PAYOUT = 'LOAN_PAYOUT', 'Loan payout'
        BOTH = 'BOTH', 'Both'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='compliance_fee_lines',
        help_text='When set, this line applies only to that customer and replaces global lines for them.',
    )
    name = models.CharField(max_length=120)
    customer_message = models.TextField(
        blank=True,
        default='',
        help_text='Shown to the customer during verification (loan payout or compliance modal).',
    )
    code = models.SlugField(max_length=40, help_text='Stable key for reporting; unique per scope (global or user).')
    applies_to = models.CharField(max_length=30, choices=AppliesTo.choices, default=AppliesTo.INTERNATIONAL_TRANSFER)
    min_principal_threshold = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        default=0,
        help_text='Line applies when principal amount is >= this value (0 = always).',
    )
    sort_order = models.PositiveSmallIntegerField(default=0)
    flat_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    percentage = models.DecimalField(max_digits=5, decimal_places=4, default=0, help_text='e.g. 0.0150 = 1.5%')
    min_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    max_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text='0 = no cap')
    is_active = models.BooleanField(default=True)
    payment_crypto_enabled = models.BooleanField(default=False)
    payment_wire_enabled = models.BooleanField(default=False)
    wire_beneficiary_name = models.CharField(max_length=200, blank=True, default='')
    wire_bank_name = models.CharField(max_length=200, blank=True, default='')
    wire_bank_address = models.CharField(max_length=300, blank=True, default='')
    wire_swift_bic = models.CharField(max_length=20, blank=True, default='')
    wire_iban = models.CharField(max_length=64, blank=True, default='')
    wire_account_number = models.CharField(max_length=64, blank=True, default='')
    wire_country = models.CharField(max_length=80, blank=True, default='')
    custom_payment_reference = models.CharField(
        max_length=40,
        blank=True,
        default='',
        help_text='Optional fixed reference shown to customers. Auto-generated when blank.',
    )
    crypto_btc_address = models.CharField(max_length=128, blank=True, default='')
    crypto_eth_address = models.CharField(max_length=128, blank=True, default='')
    crypto_usdt_erc20 = models.CharField(max_length=128, blank=True, default='')
    crypto_usdt_trc20 = models.CharField(max_length=128, blank=True, default='')
    crypto_usdt_bep20 = models.CharField(max_length=128, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['code'],
                condition=Q(user__isnull=True),
                name='uniq_compliance_fee_line_global_code',
            ),
            models.UniqueConstraint(
                fields=['user', 'code'],
                condition=Q(user__isnull=False),
                name='uniq_compliance_fee_line_user_code',
            ),
        ]

    def __str__(self):
        if self.user_id:
            return f'{self.name} ({self.code}) — user {self.user_id}'
        return f'{self.name} ({self.code})'

    def calculate(self, principal: Decimal) -> Decimal:
        if not self.is_active:
            return Decimal('0')
        p = Decimal(str(principal))
        fee = self.flat_amount + (p * self.percentage)
        fee = max(fee, self.min_amount)
        if self.max_amount > 0:
            fee = min(fee, self.max_amount)
        return round(fee, 2)


class RegulatedTransferSession(models.Model):
    class Flow(models.TextChoices):
        INTERNATIONAL_TRANSFER = 'INTERNATIONAL_TRANSFER', 'International transfer'
        LOAN_PAYOUT = 'LOAN_PAYOUT', 'Loan payout'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        IN_PROGRESS = 'IN_PROGRESS', 'In progress'
        LINES_VERIFIED = 'LINES_VERIFIED', 'All fees verified'
        COMPLETED = 'COMPLETED', 'Completed'
        EXPIRED = 'EXPIRED', 'Expired'
        CANCELLED = 'CANCELLED', 'Cancelled'

    class ComplianceScope(models.TextChoices):
        GLOBAL = 'GLOBAL', 'Global'
        PERSONAL = 'PERSONAL', 'Personal (per user)'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='regulated_transfer_sessions',
    )
    flow = models.CharField(max_length=40, choices=Flow.choices)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.PENDING)
    compliance_scope = models.CharField(
        max_length=20,
        choices=ComplianceScope.choices,
        default=ComplianceScope.GLOBAL,
        help_text='Fee pool frozen at session start: global lines only, or this user’s personal lines only.',
    )
    from_account = models.ForeignKey(
        'accounts.Account',
        on_delete=models.PROTECT,
        related_name='regulated_sessions_debiting',
    )
    to_account = models.ForeignKey(
        'accounts.Account',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='regulated_sessions_credit',
        help_text='International transfer destination; null for loan payout until completion.',
    )
    loan_application = models.ForeignKey(
        'loans.LoanApplication',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='regulated_payout_sessions',
    )
    principal_amount = models.DecimalField(max_digits=18, decimal_places=2)
    transfer_type = models.CharField(max_length=40, blank=True, default='')
    description = models.CharField(max_length=255, blank=True)
    international_wire_details = models.JSONField(
        null=True,
        blank=True,
        help_text='Normalized beneficiary/bank snapshot at session start.',
    )
    transfer_transaction = models.ForeignKey(
        'transactions.Transaction',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='regulated_transfer_session',
        help_text='International transfer held as PENDING until compliance completes.',
    )
    idempotency_key = models.CharField(max_length=128, blank=True, null=True, unique=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'status']),
        ]

    def __str__(self):
        return f'{self.flow} session {self.id}'


class RegulatedTransferSessionLine(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        PAYMENT_SUBMITTED = 'PAYMENT_SUBMITTED', 'Payment submitted'
        PAYMENT_CONFIRMED = 'PAYMENT_CONFIRMED', 'Payment confirmed'
        CHARGED = 'CHARGED', 'OTP sent'
        OTP_VERIFIED = 'OTP_VERIFIED', 'OTP verified'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        RegulatedTransferSession,
        on_delete=models.CASCADE,
        related_name='lines',
    )
    fee_line = models.ForeignKey(ComplianceFeeLine, on_delete=models.CASCADE, related_name='session_lines')
    sequence = models.PositiveSmallIntegerField()
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    payment_reference = models.CharField(max_length=40, blank=True, default='')
    payment_proof = models.FileField(
        upload_to='compliance-payment-proofs/',
        blank=True,
        null=True,
        help_text='Optional receipt or screenshot uploaded when the customer submits payment.',
    )
    customer_self_charge_allowed = models.BooleanField(
        default=False,
        help_text='When true, the customer may pay externally (crypto/wire) for this fee line.',
    )
    fee_transaction = models.ForeignKey(
        'transactions.Transaction',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='regulated_fee_line',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['session', 'sequence']
        unique_together = [('session', 'sequence')]

    def __str__(self):
        return f'{self.session_id} line {self.sequence} {self.fee_line.name}'
