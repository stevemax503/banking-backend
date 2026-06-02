import uuid
from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.utils import timezone


class CustomUserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('Email is required')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('role', CustomUser.Role.SUPER_ADMIN)
        return self.create_user(email, password, **extra_fields)


class CustomUser(AbstractBaseUser, PermissionsMixin):
    class Role(models.TextChoices):
        CUSTOMER = 'CUSTOMER', 'Customer'
        SUPER_ADMIN = 'SUPER_ADMIN', 'Super Admin'
        ADMIN = 'ADMIN', 'Admin'
        # Legacy roles (migrated to ADMIN); kept so existing rows stay valid until migration runs.
        OPERATIONS_TELLER = 'OPERATIONS_TELLER', 'Operations / Teller'
        COMPLIANCE_AUDITOR = 'COMPLIANCE_AUDITOR', 'Compliance / Auditor'
        LOAN_OFFICER = 'LOAN_OFFICER', 'Loan Officer'
        SUPPORT_STAFF = 'SUPPORT_STAFF', 'Support Staff'

    STAFF_ROLES = (Role.SUPER_ADMIN, Role.ADMIN)
    LEGACY_STAFF_ROLES = (
        Role.OPERATIONS_TELLER,
        Role.COMPLIANCE_AUDITOR,
        Role.LOAN_OFFICER,
        Role.SUPPORT_STAFF,
    )

    class KYCStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        SUBMITTED = 'SUBMITTED', 'Submitted'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'

    class IntendedAccountType(models.TextChoices):
        SAVINGS = 'SAVINGS', 'Savings'
        CHECKING = 'CHECKING', 'Checking'
        BUSINESS = 'BUSINESS', 'Business'
        FIXED_TERM = 'FIXED_TERM', 'Fixed deposit'
        CREDIT = 'CREDIT', 'Credit'

    class IdDocumentType(models.TextChoices):
        PASSPORT = 'PASSPORT', 'International passport'
        DRIVERS_LICENSE = 'DRIVERS_LICENSE', "Driver's license"
        RESIDENCE_PERMIT = 'RESIDENCE_PERMIT', 'Residence permit'
        OTHER = 'OTHER', 'Other'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=255)
    phone = models.CharField(max_length=20, blank=True)
    role = models.CharField(max_length=30, choices=Role.choices, default=Role.CUSTOMER)
    kyc_status = models.CharField(max_length=20, choices=KYCStatus.choices, default=KYCStatus.PENDING)
    kyc_document = models.FileField(upload_to='kyc/', blank=True, null=True)
    profile_picture = models.ImageField(upload_to='profiles/', blank=True, null=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_mfa_enabled = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)
    last_login = models.DateTimeField(null=True, blank=True)
    failed_login_attempts = models.PositiveIntegerField(default=0)
    is_locked = models.BooleanField(default=False)
    address = models.TextField(blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    nationality = models.CharField(max_length=100, blank=True)
    profile_setup_completed = models.BooleanField(default=False)
    intended_account_type = models.CharField(
        max_length=20,
        choices=IntendedAccountType.choices,
        blank=True,
    )
    id_document_type = models.CharField(
        max_length=30,
        choices=IdDocumentType.choices,
        blank=True,
    )
    id_document_number = models.CharField(max_length=64, blank=True)

    class AdminAccessScope(models.TextChoices):
        ALL = 'ALL', 'All customers'
        SELECTED = 'SELECTED', 'Selected customers only'

    admin_account_scope = models.CharField(
        max_length=20,
        choices=AdminAccessScope.choices,
        default=AdminAccessScope.ALL,
        help_text='Admin desk only: ALL = every customer; SELECTED = assigned customers (all their accounts).',
    )
    compliance_fees_exempt = models.BooleanField(
        default=False,
        help_text='When true, skip all compliance fee lines for international transfers and loan payouts.',
    )

    objects = CustomUserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['full_name']

    class Meta:
        verbose_name = 'User'
        verbose_name_plural = 'Users'
        ordering = ['-date_joined']

    def __str__(self):
        return f'{self.full_name} ({self.email})'

    @property
    def is_customer(self):
        return self.role == self.Role.CUSTOMER

    @property
    def is_admin_user(self):
        return self.role in self.STAFF_ROLES or self.role in self.LEGACY_STAFF_ROLES

    @classmethod
    def is_staff_role(cls, role: str) -> bool:
        return role in cls.STAFF_ROLES or role in cls.LEGACY_STAFF_ROLES

    def has_required_profile_fields(self) -> bool:
        if not self.is_customer:
            return True
        return bool(
            (self.phone or '').strip()
            and (self.address or '').strip()
            and self.date_of_birth
            and self.intended_account_type
            and self.profile_picture
            and self.id_document_type
            and (self.id_document_number or '').strip()
        )

    def try_complete_profile_setup(self):
        if not self.is_customer:
            if not self.profile_setup_completed:
                self.profile_setup_completed = True
                self.save(update_fields=['profile_setup_completed'])
            return
        if self.has_required_profile_fields():
            from apps.accounts.services import (
                provision_primary_bank_account,
                sync_primary_account_type_from_profile,
            )
            provision_primary_bank_account(self)
            sync_primary_account_type_from_profile(self)
            if self.accounts.exists():
                if not self.profile_setup_completed:
                    self.profile_setup_completed = True
                    self.save(update_fields=['profile_setup_completed'])
            elif self.profile_setup_completed:
                self.profile_setup_completed = False
                self.save(update_fields=['profile_setup_completed'])

    @property
    def has_unrestricted_admin_access(self):
        if self.role == self.Role.SUPER_ADMIN:
            return True
        return self.admin_account_scope == self.AdminAccessScope.ALL


class StaffCustomerAssignment(models.Model):
    """Links an admin user to customers they may manage (all of each customer's accounts)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    staff = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='customer_assignments',
    )
    customer = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='assigned_admins',
        limit_choices_to={'role': CustomUser.Role.CUSTOMER},
    )
    assigned_at = models.DateTimeField(default=timezone.now)
    assigned_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='customer_assignments_made',
    )

    class Meta:
        unique_together = [('staff', 'customer')]
        ordering = ['-assigned_at']

    def __str__(self):
        return f'{self.staff.email} → {self.customer.email}'


class ProfileChangeRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        'CustomUser',
        on_delete=models.CASCADE,
        related_name='profile_change_requests',
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    proposed_full_name = models.CharField(max_length=255)
    proposed_phone = models.CharField(max_length=20, blank=True)
    proposed_address = models.TextField(blank=True)
    proposed_date_of_birth = models.DateField(null=True, blank=True)
    proposed_nationality = models.CharField(max_length=100, blank=True)
    proposed_email = models.EmailField(blank=True)
    proposed_id_document_type = models.CharField(max_length=30, blank=True)
    proposed_id_document_number = models.CharField(max_length=64, blank=True)
    proposed_profile_picture = models.ImageField(
        upload_to='profile_requests/',
        blank=True,
        null=True,
    )
    rejection_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        'CustomUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_profile_requests',
    )

    class Meta:
        ordering = ['-created_at']

    def apply_to_user(self):
        user = self.user
        user.full_name = self.proposed_full_name
        user.phone = self.proposed_phone
        user.address = self.proposed_address
        if self.proposed_date_of_birth is not None:
            user.date_of_birth = self.proposed_date_of_birth
        user.nationality = self.proposed_nationality
        if self.proposed_email and self.proposed_email.strip():
            email_norm = self.proposed_email.strip().lower()
            if (
                CustomUser.objects.filter(email__iexact=email_norm)
                .exclude(pk=user.pk)
                .exists()
            ):
                raise ValueError('Email already in use')
            user.email = email_norm
        if self.proposed_id_document_type:
            user.id_document_type = self.proposed_id_document_type
        if self.proposed_id_document_number:
            user.id_document_number = self.proposed_id_document_number
        if self.proposed_profile_picture:
            user.profile_picture = self.proposed_profile_picture
        user.save()


class PasswordResetToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='password_reset_tokens')
    token = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']

    def is_valid(self):
        return not self.is_used and timezone.now() < self.expires_at


class EmailOTPToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='email_otp_tokens')
    token = models.CharField(max_length=6)
    purpose = models.CharField(max_length=30)  # 'login_mfa', 'transaction_verify', etc.
    context_id = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
        help_text='Optional scope (e.g. regulated fee line id) so multiple OTPs can coexist per user.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)

    class Meta:
        ordering = ['-created_at']

    def is_valid(self):
        if self.is_used:
            return False
        if not self.expires_at:
            return False
        return timezone.now() < self.expires_at
