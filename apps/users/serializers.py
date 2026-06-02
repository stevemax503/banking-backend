from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from .models import EmailOTPToken, ProfileChangeRequest

User = get_user_model()


class UserRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, validators=[validate_password])
    password_confirm = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ['email', 'full_name', 'phone', 'password', 'password_confirm']

    def validate_email(self, value):
        email = User.objects.normalize_email(value)
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError(
                'An account with this email already exists. Please sign in instead.',
            )
        return email

    def validate(self, attrs):
        if attrs['password'] != attrs['password_confirm']:
            raise serializers.ValidationError({'password': 'Passwords do not match.'})
        return attrs

    def create(self, validated_data):
        validated_data.pop('password_confirm')
        validated_data['profile_setup_completed'] = False
        return User.objects.create_user(**validated_data)


class UserProfileSerializer(serializers.ModelSerializer):
    requires_profile_setup = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'email', 'full_name', 'phone', 'role', 'kyc_status',
            'profile_picture', 'address', 'date_of_birth', 'nationality',
            'is_mfa_enabled', 'date_joined', 'last_login',
            'profile_setup_completed', 'intended_account_type', 'id_document_type',
            'id_document_number', 'requires_profile_setup', 'admin_account_scope',
        ]
        read_only_fields = [
            'id', 'email', 'role', 'kyc_status', 'date_joined', 'last_login',
            'profile_setup_completed', 'requires_profile_setup',
        ]

    def get_requires_profile_setup(self, obj):
        if not obj.is_customer:
            return False
        # Field completeness + a provisioned bank account (primary 16-digit account).
        if not obj.has_required_profile_fields():
            return True
        return not obj.accounts.exists()


class UserProfileUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            'full_name', 'phone', 'address', 'date_of_birth', 'nationality', 'profile_picture',
            'intended_account_type', 'id_document_type', 'id_document_number',
        ]


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, validators=[validate_password])
    new_password_confirm = serializers.CharField(write_only=True)

    def validate(self, attrs):
        if attrs['new_password'] != attrs['new_password_confirm']:
            raise serializers.ValidationError({'new_password': 'Passwords do not match.'})
        return attrs

    def validate_current_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError('Current password is incorrect.')
        return value


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    token = serializers.CharField()
    new_password = serializers.CharField(validators=[validate_password])
    new_password_confirm = serializers.CharField()

    def validate(self, attrs):
        if attrs['new_password'] != attrs['new_password_confirm']:
            raise serializers.ValidationError({'new_password': 'Passwords do not match.'})
        return attrs


class MFAEnrollSerializer(serializers.Serializer):
    mfa_type = serializers.ChoiceField(choices=['totp', 'email'])


class MFAVerifySerializer(serializers.Serializer):
    token = serializers.CharField(max_length=10)
    mfa_type = serializers.ChoiceField(choices=['totp', 'email'])


class KYCUploadSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['kyc_document']

    def update(self, instance, validated_data):
        instance.kyc_document = validated_data.get('kyc_document', instance.kyc_document)
        instance.kyc_status = User.KYCStatus.SUBMITTED
        instance.save()
        return instance


class AdminUserSerializer(serializers.ModelSerializer):
    assigned_customers = serializers.SerializerMethodField()
    assigned_customer_ids = serializers.ListField(
        child=serializers.UUIDField(),
        write_only=True,
        required=False,
        allow_empty=True,
    )

    class Meta:
        model = User
        fields = [
            'id', 'email', 'full_name', 'phone', 'role', 'kyc_status',
            'is_active', 'is_locked', 'is_mfa_enabled', 'date_joined', 'last_login',
            'address', 'date_of_birth', 'nationality',
            'admin_account_scope', 'assigned_customers', 'assigned_customer_ids',
            'compliance_fees_exempt',
        ]
        read_only_fields = ['id', 'date_joined', 'last_login', 'assigned_customers']

    def validate_compliance_fees_exempt(self, value):
        instance = getattr(self, 'instance', None)
        role = self.initial_data.get('role') if instance is None else instance.role
        if value and role and role != User.Role.CUSTOMER:
            raise serializers.ValidationError('Only customers can be exempt from compliance fees.')
        return value

    def get_assigned_customers(self, obj):
        if obj.role == User.Role.CUSTOMER:
            return []
        from apps.admin_portal.scoping import assigned_customers_payload

        return assigned_customers_payload(obj)

    def validate(self, attrs):
        scope = attrs.get('admin_account_scope', getattr(self.instance, 'admin_account_scope', None))
        customer_ids = self.initial_data.get('assigned_customer_ids')
        role = attrs.get('role', getattr(self.instance, 'role', None))
        if role and role != User.Role.CUSTOMER and scope == User.AdminAccessScope.SELECTED:
            if customer_ids is not None and len(customer_ids) == 0:
                raise serializers.ValidationError(
                    {'assigned_customer_ids': 'Select at least one customer for selected scope.'},
                )
            if self.instance is None and customer_ids is None:
                raise serializers.ValidationError(
                    {'assigned_customer_ids': 'Required when user scope is selected.'},
                )
        return attrs


STAFF_ROLES = [User.Role.SUPER_ADMIN, User.Role.ADMIN]


class AdminCreateStaffUserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, validators=[validate_password])
    password_confirm = serializers.CharField(write_only=True)
    assigned_customer_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=True,
    )

    class Meta:
        model = User
        fields = [
            'email', 'full_name', 'phone', 'password', 'password_confirm', 'role',
            'admin_account_scope', 'assigned_customer_ids',
        ]

    def validate_role(self, value):
        if value not in [r.value for r in STAFF_ROLES]:
            raise serializers.ValidationError('Role must be Admin or Super Admin.')
        return value

    def validate(self, attrs):
        if attrs['password'] != attrs['password_confirm']:
            raise serializers.ValidationError({'password': 'Passwords do not match.'})
        scope = attrs.get('admin_account_scope', User.AdminAccessScope.ALL)
        customer_ids = attrs.get('assigned_customer_ids') or []
        role = attrs.get('role')
        if role == User.Role.SUPER_ADMIN:
            attrs['admin_account_scope'] = User.AdminAccessScope.ALL
        elif role == User.Role.ADMIN and scope == User.AdminAccessScope.SELECTED and not customer_ids:
            raise serializers.ValidationError(
                {'assigned_customer_ids': 'Select at least one customer for selected scope.'},
            )
        return attrs

    def create(self, validated_data):
        customer_ids = validated_data.pop('assigned_customer_ids', [])
        validated_data.pop('password_confirm')
        role = validated_data.get('role', User.Role.ADMIN)
        if role == User.Role.SUPER_ADMIN:
            validated_data['admin_account_scope'] = User.AdminAccessScope.ALL
        user = User.objects.create_user(
            **validated_data,
            is_staff=True,
            kyc_status=User.KYCStatus.APPROVED,
            profile_setup_completed=True,
        )
        if role == User.Role.SUPER_ADMIN:
            user.is_superuser = True
            user.save(update_fields=['is_superuser'])
        elif user.admin_account_scope == User.AdminAccessScope.SELECTED:
            from apps.admin_portal.scoping import set_staff_customer_assignments

            request = self.context.get('request')
            set_staff_customer_assignments(user, customer_ids, request.user if request else None)
        return user


class ProfileChangeRequestCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProfileChangeRequest
        fields = [
            'proposed_full_name', 'proposed_phone', 'proposed_address',
            'proposed_date_of_birth', 'proposed_nationality', 'proposed_email',
            'proposed_id_document_type', 'proposed_id_document_number',
            'proposed_profile_picture',
        ]

    def validate(self, attrs):
        req = self.context['request']
        pic = req.FILES.get('proposed_profile_picture')
        has_text = any(
            [
                (attrs.get('proposed_full_name') or '').strip(),
                (attrs.get('proposed_phone') or '').strip(),
                (attrs.get('proposed_address') or '').strip(),
                (attrs.get('proposed_nationality') or '').strip(),
                (attrs.get('proposed_email') or '').strip(),
                (attrs.get('proposed_id_document_type') or '').strip(),
                (attrs.get('proposed_id_document_number') or '').strip(),
            ]
        )
        if not pic and not has_text and not attrs.get('proposed_date_of_birth'):
            raise serializers.ValidationError('Submit at least one change.')
        return attrs

    def create(self, validated_data):
        validated_data['user'] = self.context['request'].user
        return super().create(validated_data)


class ProfileChangeRequestAdminSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)
    user_name = serializers.CharField(source='user.full_name', read_only=True)

    class Meta:
        model = ProfileChangeRequest
        fields = [
            'id', 'user', 'user_email', 'user_name', 'status',
            'proposed_full_name', 'proposed_phone', 'proposed_address',
            'proposed_date_of_birth', 'proposed_nationality', 'proposed_email',
            'proposed_id_document_type', 'proposed_id_document_number',
            'proposed_profile_picture',
            'rejection_reason', 'created_at', 'reviewed_at', 'reviewed_by',
        ]
        read_only_fields = [
            'id', 'user', 'user_email', 'user_name', 'status',
            'proposed_full_name', 'proposed_phone', 'proposed_address',
            'proposed_date_of_birth', 'proposed_nationality', 'proposed_email',
            'proposed_id_document_type', 'proposed_id_document_number',
            'proposed_profile_picture',
            'rejection_reason', 'created_at', 'reviewed_at', 'reviewed_by',
        ]


class EmailOTPTokenAdminSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)
    user_name = serializers.CharField(source='user.full_name', read_only=True)
    purpose_label = serializers.SerializerMethodField()
    is_expired = serializers.SerializerMethodField()
    fee_line_name = serializers.SerializerMethodField()

    class Meta:
        model = EmailOTPToken
        fields = [
            'id', 'user_email', 'user_name', 'purpose', 'purpose_label', 'token', 'context_id',
            'fee_line_name',
            'created_at', 'expires_at', 'is_used', 'is_expired',
        ]
        read_only_fields = fields

    def get_purpose_label(self, obj):
        from .email_otp import otp_purpose_label

        return otp_purpose_label(obj.purpose)

    def get_is_expired(self, obj):
        from django.utils import timezone

        if not obj.expires_at:
            return True
        return obj.expires_at < timezone.now()

    def get_fee_line_name(self, obj):
        from .email_otp import PURPOSE_REGULATED_FEE

        if obj.purpose != PURPOSE_REGULATED_FEE or not obj.context_id:
            return None
        from apps.transactions.regulated_models import RegulatedTransferSessionLine

        ln = (
            RegulatedTransferSessionLine.objects.filter(pk=obj.context_id)
            .select_related('fee_line')
            .first()
        )
        return ln.fee_line.name if ln and ln.fee_line_id else None
