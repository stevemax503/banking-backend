from decimal import Decimal

from rest_framework import serializers
from .deposit_source import DepositMethod, TransactionError as DepositSourceError, normalize_deposit_source
from .models import Transaction, TransactionFee, ExchangeRate
from apps.users.models import CustomUser
from .regulated_models import ComplianceFeeLine

_MIN_AMOUNT = Decimal('0.01')


class BlankAsZeroDecimalField(serializers.DecimalField):
    """Treat empty string from HTML number inputs as zero."""

    def to_internal_value(self, data):
        if data is None or (isinstance(data, str) and not data.strip()):
            data = '0'
        return super().to_internal_value(data)


def _normalize_description(attrs: dict, default: str) -> dict:
    raw = attrs.get('description')
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        attrs['description'] = default
    else:
        attrs['description'] = raw.strip()
    return attrs


def _validate_domestic_transfer_destination(attrs: dict, *, resolve_in_database: bool) -> dict:
    """Internal/external: UAE IBAN or 11–16 digit account; optional DB resolve on submit."""
    from apps.accounts.models import Account
    from .services import TransactionError, normalize_destination_account_number, resolve_account_by_identifier

    try:
        to_number = normalize_destination_account_number(attrs['to_account_id'])
    except TransactionError as e:
        raise serializers.ValidationError({'to_account_id': str(e)}) from e

    attrs['to_account_number'] = to_number

    try:
        from_acc = Account.objects.get(id=attrs['from_account_id'])
    except Account.DoesNotExist:
        raise serializers.ValidationError({'from_account_id': 'Source account not found.'})

    if from_acc.account_number == to_number:
        raise serializers.ValidationError('Source and destination accounts must be different.')

    if resolve_in_database:
        dest = resolve_account_by_identifier(to_number)
        if not dest:
            raise serializers.ValidationError(
                {
                    'to_account_id': (
                        'Destination account not found. Check the account number and try again.'
                    ),
                },
            )
        attrs['to_account_resolved_id'] = str(dest.id)

    return attrs


def _validate_transfer_destination(attrs: dict, *, resolve_in_database: bool) -> dict:
    from .services import resolve_account_by_identifier

    tt = attrs.get('transfer_type', Transaction.TransactionType.TRANSFER_INTERNAL)
    if tt in (
        Transaction.TransactionType.TRANSFER_INTERNAL,
        Transaction.TransactionType.TRANSFER_EXTERNAL,
    ):
        attrs = _validate_domestic_transfer_destination(attrs, resolve_in_database=resolve_in_database)
    else:
        from .services import TransactionError, normalize_destination_account_number

        try:
            to_number = normalize_destination_account_number(attrs['to_account_id'])
        except TransactionError as e:
            raise serializers.ValidationError({'to_account_id': str(e)}) from e
        attrs['to_account_number'] = to_number
        if resolve_in_database:
            dest = resolve_account_by_identifier(to_number)
            if not dest:
                raise serializers.ValidationError(
                    {
                        'to_account_id': (
                            'Destination account not found. Enter the account number or ID from the recipient.'
                        ),
                    },
                )
            to_id = str(dest.id)
            if str(attrs['from_account_id']) == to_id:
                raise serializers.ValidationError('Source and destination accounts must be different.')
            attrs['to_account_resolved_id'] = to_id

    return _apply_international_wire_validation(attrs)


def _validate_recipient_fields(attrs: dict) -> dict:
    tt = attrs.get('transfer_type', Transaction.TransactionType.TRANSFER_INTERNAL)
    if tt not in (
        Transaction.TransactionType.TRANSFER_INTERNAL,
        Transaction.TransactionType.TRANSFER_EXTERNAL,
    ):
        return attrs

    name = (attrs.get('account_holder_name') or '').strip()
    if len(name) < 2:
        raise serializers.ValidationError(
            {'account_holder_name': 'Enter the account holder name (at least 2 characters).'},
        )
    attrs['account_holder_name'] = name

    if tt == Transaction.TransactionType.TRANSFER_EXTERNAL:
        bank = (attrs.get('external_bank_name') or '').strip()
        if len(bank) < 2:
            raise serializers.ValidationError(
                {'external_bank_name': 'Enter the recipient bank name (at least 2 characters).'},
            )
        attrs['external_bank_name'] = bank
    else:
        attrs.pop('external_bank_name', None)

    return attrs


def _apply_international_wire_validation(attrs: dict) -> dict:
    """Require normalized wire details for international transfers; forbid the payload on other types."""
    from .intl_wire import validate_and_normalize_international_details

    tt = attrs.get('transfer_type', Transaction.TransactionType.TRANSFER_INTERNAL)
    raw = attrs.get('international_details')
    if tt == Transaction.TransactionType.TRANSFER_INTERNATIONAL:
        attrs['international_wire'] = validate_and_normalize_international_details(raw)
    else:
        if raw is not None:
            raise serializers.ValidationError(
                {'international_details': 'Only include this object for international transfers.'},
            )
        attrs['international_wire'] = None
    attrs.pop('international_details', None)
    return attrs


class TransactionSerializer(serializers.ModelSerializer):
    from_account_number = serializers.CharField(source='from_account.account_number', read_only=True, allow_null=True)
    to_account_number = serializers.SerializerMethodField()
    initiated_by_name = serializers.CharField(source='initiated_by.full_name', read_only=True)
    initiated_by_email = serializers.CharField(source='initiated_by.email', read_only=True)
    customer_email = serializers.SerializerMethodField()
    regulated_session_id = serializers.SerializerMethodField()
    compliance_resume = serializers.SerializerMethodField()

    def get_customer_email(self, obj):
        if obj.to_account_id and obj.to_account:
            return obj.to_account.owner.email
        if obj.from_account_id and obj.from_account:
            return obj.from_account.owner.email
        return ''

    def get_to_account_number(self, obj):
        if obj.to_account_id and obj.to_account:
            return obj.to_account.account_number
        meta = obj.metadata or {}
        return meta.get('destination_account_number') or ''

    def get_regulated_session_id(self, obj):
        resume = self.get_compliance_resume(obj)
        return resume.get('session_id') if resume else None

    def get_compliance_resume(self, obj):
        from .regulated_flow import compliance_resume_for_transaction

        return compliance_resume_for_transaction(obj)

    class Meta:
        model = Transaction
        fields = [
            'id', 'reference_number', 'transaction_type', 'amount', 'currency',
            'from_account', 'from_account_number', 'to_account', 'to_account_number',
            'status', 'description', 'fee_amount', 'exchange_rate', 'metadata',
            'regulated_session_id', 'compliance_resume',
            'initiated_by_name', 'initiated_by_email', 'customer_email',
            'created_at', 'completed_at',
        ]
        read_only_fields = fields


class DepositSerializer(serializers.Serializer):
    account_id = serializers.UUIDField()
    amount = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=_MIN_AMOUNT)
    description = serializers.CharField(max_length=255, required=False, allow_blank=True, default='Deposit')
    idempotency_key = serializers.CharField(max_length=128, required=False, allow_blank=True)


class AdminDepositSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=_MIN_AMOUNT)
    description = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')
    deposit_method = serializers.ChoiceField(choices=DepositMethod.CHOICES, default=DepositMethod.TRANSFER)
    deposit_source = serializers.DictField(
        child=serializers.CharField(allow_blank=True, max_length=200),
        required=False,
        default=dict,
    )
    status = serializers.ChoiceField(
        choices=Transaction.Status.choices,
        required=False,
        default=Transaction.Status.COMPLETED,
    )
    transaction_at = serializers.DateTimeField(required=False, allow_null=True)

    def validate(self, attrs):
        from .narration import normalize_deposit_source_optional

        attrs['deposit_source'] = normalize_deposit_source_optional(
            attrs['deposit_method'],
            attrs.get('deposit_source'),
        )
        return attrs


class AdminAccountDebitSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=_MIN_AMOUNT)
    transfer_type = serializers.ChoiceField(
        choices=[
            Transaction.TransactionType.TRANSFER_INTERNAL,
            Transaction.TransactionType.TRANSFER_EXTERNAL,
            Transaction.TransactionType.TRANSFER_INTERNATIONAL,
        ],
    )
    description = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')
    status = serializers.ChoiceField(
        choices=Transaction.Status.choices,
        required=False,
        default=Transaction.Status.COMPLETED,
    )
    transaction_at = serializers.DateTimeField(required=False, allow_null=True)
    to_account_id = serializers.CharField(max_length=80, required=False, allow_blank=True)
    beneficiary_name = serializers.CharField(max_length=140, required=False, allow_blank=True)
    bank_name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    account_number = serializers.CharField(max_length=80, required=False, allow_blank=True)
    international_details = serializers.DictField(
        child=serializers.CharField(allow_blank=True, max_length=200),
        required=False,
        default=dict,
    )


class AdminTransactionUpdateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=_MIN_AMOUNT, required=False)
    status = serializers.ChoiceField(choices=Transaction.Status.choices, required=False)
    description = serializers.CharField(max_length=255, required=False, allow_blank=True)
    transaction_type = serializers.ChoiceField(choices=Transaction.TransactionType.choices, required=False)
    fee_amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0'), required=False)
    currency = serializers.CharField(max_length=3, required=False)
    transaction_at = serializers.DateTimeField(required=False, allow_null=True)


class AdminTransactionBulkDeleteSerializer(serializers.Serializer):
    ids = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=100)


class WithdrawSerializer(serializers.Serializer):
    account_id = serializers.UUIDField()
    amount = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=_MIN_AMOUNT)
    description = serializers.CharField(max_length=255, required=False, allow_blank=True, default='Withdrawal')
    idempotency_key = serializers.CharField(max_length=128, required=False, allow_blank=True)


class TransferSerializer(serializers.Serializer):
    from_account_id = serializers.UUIDField()
    to_account_id = serializers.CharField(max_length=80, trim_whitespace=True)
    amount = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=_MIN_AMOUNT)
    description = serializers.CharField(max_length=255, required=False, allow_blank=True, default='Transfer')
    transfer_type = serializers.ChoiceField(
        choices=['TRANSFER_INTERNAL', 'TRANSFER_EXTERNAL', 'TRANSFER_INTERNATIONAL'],
        default='TRANSFER_INTERNAL',
    )
    account_holder_name = serializers.CharField(max_length=140, required=False, allow_blank=True)
    external_bank_name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    idempotency_key = serializers.CharField(max_length=128, required=False, allow_blank=True)
    otp = serializers.CharField(max_length=6, required=False, allow_blank=True)
    regulated_session_id = serializers.UUIDField(required=False, allow_null=True)
    international_details = serializers.JSONField(required=False, allow_null=True)

    def validate(self, attrs):
        attrs = _validate_recipient_fields(attrs)
        attrs = _validate_transfer_destination(attrs, resolve_in_database=False)
        tt = attrs.get('transfer_type', Transaction.TransactionType.TRANSFER_INTERNAL)
        default = 'International transfer' if tt == Transaction.TransactionType.TRANSFER_INTERNATIONAL else 'Transfer'
        return _normalize_description(attrs, default)


class TransferPreviewSerializer(serializers.Serializer):
    from_account_id = serializers.UUIDField()
    to_account_id = serializers.CharField(max_length=80, trim_whitespace=True)
    amount = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=_MIN_AMOUNT)
    transfer_type = serializers.ChoiceField(
        choices=['TRANSFER_INTERNAL', 'TRANSFER_EXTERNAL', 'TRANSFER_INTERNATIONAL'],
        default='TRANSFER_INTERNAL',
    )
    international_details = serializers.JSONField(required=False, allow_null=True)

    def validate(self, attrs):
        return _validate_transfer_destination(attrs, resolve_in_database=False)


class TransferSendOtpSerializer(serializers.Serializer):
    """Same shape as transfer preview — used to email a transfer verification code."""

    from_account_id = serializers.UUIDField()
    to_account_id = serializers.CharField(max_length=80, trim_whitespace=True)
    amount = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=_MIN_AMOUNT)
    transfer_type = serializers.ChoiceField(
        choices=['TRANSFER_INTERNAL', 'TRANSFER_EXTERNAL', 'TRANSFER_INTERNATIONAL'],
        default='TRANSFER_INTERNAL',
    )
    international_details = serializers.JSONField(required=False, allow_null=True)

    def validate(self, attrs):
        return _validate_transfer_destination(attrs, resolve_in_database=False)


class TransactionFeeSerializer(serializers.ModelSerializer):
    class Meta:
        model = TransactionFee
        fields = [
            'id',
            'fee_type',
            'flat_amount',
            'percentage',
            'min_amount',
            'max_amount',
            'is_active',
            'requires_otp',
            'charge_upfront',
            'updated_at',
        ]
        read_only_fields = ['id', 'updated_at']


class ExchangeRateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExchangeRate
        fields = ['id', 'from_currency', 'to_currency', 'rate', 'fetched_at']


class ComplianceFeeLineSerializer(serializers.ModelSerializer):
    user = serializers.PrimaryKeyRelatedField(
        queryset=CustomUser.objects.filter(role=CustomUser.Role.CUSTOMER),
        allow_null=True,
        required=False,
    )
    user_email = serializers.EmailField(source='user.email', read_only=True, allow_null=True)
    user_full_name = serializers.CharField(source='user.full_name', read_only=True, allow_null=True)
    scope = serializers.SerializerMethodField()
    min_principal_threshold = BlankAsZeroDecimalField(max_digits=18, decimal_places=2, required=False)
    flat_amount = BlankAsZeroDecimalField(max_digits=10, decimal_places=2, required=False)
    percentage = BlankAsZeroDecimalField(max_digits=5, decimal_places=4, required=False)
    min_amount = BlankAsZeroDecimalField(max_digits=10, decimal_places=2, required=False)
    max_amount = BlankAsZeroDecimalField(max_digits=10, decimal_places=2, required=False)

    class Meta:
        model = ComplianceFeeLine
        fields = [
            'id',
            'user',
            'user_email',
            'user_full_name',
            'scope',
            'name',
            'customer_message',
            'code',
            'applies_to',
            'payment_crypto_enabled',
            'payment_wire_enabled',
            'wire_beneficiary_name',
            'wire_bank_name',
            'wire_bank_address',
            'wire_swift_bic',
            'wire_iban',
            'wire_account_number',
            'wire_country',
            'custom_payment_reference',
            'crypto_btc_address',
            'crypto_eth_address',
            'crypto_usdt_erc20',
            'crypto_usdt_trc20',
            'crypto_usdt_bep20',
            'min_principal_threshold',
            'sort_order',
            'flat_amount',
            'percentage',
            'min_amount',
            'max_amount',
            'is_active',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id', 'created_at', 'updated_at', 'user_email', 'user_full_name', 'scope', 'sort_order',
        ]

    def get_scope(self, obj):
        return 'user' if obj.user_id else 'global'

    def validate(self, attrs):
        user = attrs.get('user', getattr(self.instance, 'user', None))
        code = attrs.get('code', getattr(self.instance, 'code', None))
        if code and user is None:
            qs = ComplianceFeeLine.objects.filter(code=code, user__isnull=True)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({'code': 'A global line with this code already exists.'})
        if code and user is not None:
            qs = ComplianceFeeLine.objects.filter(code=code, user=user)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({'code': 'This user already has a line with this code.'})
        ref = attrs.get(
            'custom_payment_reference',
            getattr(self.instance, 'custom_payment_reference', '') if self.instance else '',
        )
        if ref and len(str(ref).strip()) > 40:
            raise serializers.ValidationError({'custom_payment_reference': 'Must be 40 characters or fewer.'})
        return attrs

    def create(self, validated_data):
        user = validated_data.get('user')
        scope_qs = ComplianceFeeLine.objects.filter(user=user) if user else ComplianceFeeLine.objects.filter(user__isnull=True)
        max_order = scope_qs.order_by('-sort_order').values_list('sort_order', flat=True).first() or 0
        validated_data.setdefault('sort_order', int(max_order) + 10)
        return super().create(validated_data)


class RegulatedIntlSessionStartSerializer(serializers.Serializer):
    from_account_id = serializers.UUIDField()
    to_account_id = serializers.CharField(max_length=80, trim_whitespace=True)
    amount = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=_MIN_AMOUNT)
    transfer_type = serializers.ChoiceField(
        choices=['TRANSFER_INTERNATIONAL'],
        default='TRANSFER_INTERNATIONAL',
    )
    description = serializers.CharField(max_length=255, required=False, allow_blank=True)
    idempotency_key = serializers.CharField(max_length=128, required=False, allow_blank=True)
    transfer_otp = serializers.CharField(max_length=6, min_length=6)
    international_details = serializers.JSONField(required=False, allow_null=True)

    def validate(self, attrs):
        attrs = _validate_transfer_destination(attrs, resolve_in_database=False)
        return _normalize_description(attrs, 'International transfer')


class RegulatedLineOtpSerializer(serializers.Serializer):
    otp = serializers.CharField(max_length=6, min_length=6)


class LoanRegulatedPayoutStartSerializer(serializers.Serializer):
    disbursement_account_id = serializers.UUIDField()
    idempotency_key = serializers.CharField(max_length=128, required=False, allow_blank=True)


class LoanRegulatedPayoutCompleteSerializer(serializers.Serializer):
    regulated_session_id = serializers.UUIDField(required=False, allow_null=True)
    disbursement_account_id = serializers.UUIDField()
