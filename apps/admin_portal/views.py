import logging
from decimal import Decimal
from django.db.models import Q, Sum, Count
from django.utils import timezone
from rest_framework import generics, parsers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from rest_framework.pagination import PageNumberPagination
import django_filters

from .permissions import (
    IsSuperAdmin, IsAdminUser, IsOperationsTeller,
    IsLoanOfficer, IsSupportStaff,
)
from apps.users.models import CustomUser, ProfileChangeRequest, EmailOTPToken, StaffCustomerAssignment
from apps.users.force_delete import force_delete_user
logger = logging.getLogger(__name__)


def _sync_sessions_after_compliance_line_change(instance, request_user) -> None:
    """Best-effort sync; failures are logged but must not fail the admin API response."""
    try:
        from apps.transactions.regulated_flow import sync_all_active_compliance_sessions

        if instance.user_id:
            sync_all_active_compliance_sessions(user=instance.user)
        elif staff_has_unrestricted_access(request_user):
            sync_all_active_compliance_sessions()
    except Exception:
        logger.exception('Compliance session sync failed after fee line %s change', instance.pk)


from .scoping import (
    staff_has_unrestricted_access,
    staff_assigned_account_ids,
    staff_assigned_owner_ids,
    filter_accounts,
    filter_transactions,
    filter_customers,
    filter_compliance_fee_lines,
    assert_account_in_scope,
    assert_owner_in_scope,
    assert_transaction_in_scope,
    assert_can_create_global_compliance,
    assert_can_manage_global_compliance,
    set_staff_customer_assignments,
)
from apps.users.serializers import (
    AdminUserSerializer,
    AdminCreateStaffUserSerializer,
    ProfileChangeRequestAdminSerializer,
    EmailOTPTokenAdminSerializer,
    UserProfileSerializer,
)
from apps.users.email_otp import create_email_otp
from apps.notifications.services import send_email_notification
from apps.accounts.models import Account
from apps.accounts.serializers import AccountSerializer, AccountStatusSerializer
from apps.transactions.models import Transaction, ComplianceFeeLine
from apps.transactions.serializers import (
    TransactionSerializer,
    TransactionFeeSerializer,
    ComplianceFeeLineSerializer,
    AdminDepositSerializer,
    AdminAccountDebitSerializer,
    AdminTransactionUpdateSerializer,
    AdminTransactionBulkDeleteSerializer,
)
from apps.transactions.admin_transaction import (
    admin_update_transaction,
    admin_delete_transactions,
    AdminTransactionError,
)
from apps.transactions.services import (
    reverse_transaction,
    deposit,
    withdraw,
    admin_deposit,
    admin_account_debit as record_admin_account_debit,
    preview_deposit,
    AccountStatusError,
    TransactionError,
    InsufficientFundsError,
)
from apps.loans.models import LoanApplication, LoanProduct
from apps.loans.serializers import LoanApplicationSerializer
from apps.loans.admin_serializers import AdminLoanProductSerializer
from apps.loans.services import disburse_loan
from apps.loans.loan_application_pdf import (
    LoanFormPrefill,
    email_loan_application_pdf,
    generate_loan_application_pdf,
    prefill_from_user,
)
from apps.support.models import SupportTicket, TicketMessage
from apps.support.serializers import SupportTicketSerializer, AddMessageSerializer
from apps.audit.models import AuditLog, log_action
from apps.audit.serializers import AuditLogSerializer
from apps.audit.middleware import AuditMiddleware


# ── Dashboard ──────────────────────────────────────────────────────────────────

@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_dashboard(request):
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    user = request.user

    customers = CustomUser.objects.filter(role=CustomUser.Role.CUSTOMER)
    customers = filter_customers(customers, user)
    accounts = filter_accounts(Account.objects.all(), user)
    month_tx = filter_transactions(
        Transaction.objects.filter(
            created_at__gte=month_start, status=Transaction.Status.COMPLETED,
        ),
        user,
    )
    loans = LoanApplication.objects.filter(status__in=['SUBMITTED', 'UNDER_REVIEW'])
    owner_ids = staff_assigned_owner_ids(user)
    if owner_ids is not None:
        loans = loans.filter(applicant_id__in=owner_ids)
    tickets = SupportTicket.objects.filter(status__in=['OPEN', 'IN_PROGRESS'])
    if owner_ids is not None:
        tickets = tickets.filter(customer_id__in=owner_ids)
    flagged = filter_transactions(
        Transaction.objects.filter(status=Transaction.Status.FLAGGED),
        user,
    )

    data = {
        'total_users': customers.count(),
        'total_accounts': accounts.count(),
        'active_accounts': accounts.filter(status='ACTIVE').count(),
        'total_transactions_this_month': month_tx.count(),
        'transaction_volume_this_month': month_tx.aggregate(total=Sum('amount'))['total'] or 0,
        'pending_loan_applications': loans.count(),
        'open_support_tickets': tickets.count(),
        'flagged_transactions': flagged.count(),
        'scoped_access': not staff_has_unrestricted_access(user),
    }
    return Response(data)


# ── User Management ─────────────────────────────────────────────────────────────

class AdminUserListView(generics.ListAPIView):
    serializer_class = AdminUserSerializer
    permission_classes = [IsAdminUser]
    search_fields = ['email', 'full_name', 'phone']
    filterset_fields = ['role', 'kyc_status', 'is_active', 'is_locked', 'compliance_fees_exempt']

    def get_queryset(self):
        qs = CustomUser.objects.all().order_by('-date_joined')
        if staff_has_unrestricted_access(self.request.user):
            return qs
        owner_ids = staff_assigned_owner_ids(self.request.user)
        return qs.filter(
            Q(role=CustomUser.Role.CUSTOMER, id__in=owner_ids)
            | Q(id=self.request.user.id),
        )


class AdminUserDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = AdminUserSerializer
    permission_classes = [IsAdminUser]
    queryset = CustomUser.objects.all()

    def get_permissions(self):
        if self.request.method == 'DELETE':
            return [IsSuperAdmin()]
        return [IsAdminUser()]

    def perform_update(self, serializer):
        old_data = AdminUserSerializer(self.get_object()).data
        instance = serializer.save()
        customer_ids = self.request.data.get('assigned_customer_ids')
        if (
            customer_ids is not None
            and instance.role != CustomUser.Role.CUSTOMER
            and self.request.user.role == CustomUser.Role.SUPER_ADMIN
        ):
            if instance.role == CustomUser.Role.SUPER_ADMIN:
                instance.admin_account_scope = CustomUser.AdminAccessScope.ALL
                instance.save(update_fields=['admin_account_scope'])
                StaffCustomerAssignment.objects.filter(staff=instance).delete()
            elif instance.admin_account_scope == CustomUser.AdminAccessScope.SELECTED:
                set_staff_customer_assignments(instance, customer_ids, self.request.user)
            else:
                StaffCustomerAssignment.objects.filter(staff=instance).delete()
        log_action(
            actor=self.request.user,
            action=AuditLog.Action.UPDATE,
            target_model='CustomUser',
            target_id=instance.id,
            old_value=dict(old_data),
            new_value=serializer.data,
            ip_address=AuditMiddleware.get_client_ip(self.request),
        )

    def destroy(self, request, *args, **kwargs):
        user = self.get_object()
        if user.id == request.user.id:
            return Response(
                {'detail': 'You cannot delete your own account.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if user.role == CustomUser.Role.SUPER_ADMIN:
            other_admins = CustomUser.objects.filter(role=CustomUser.Role.SUPER_ADMIN).exclude(id=user.id)
            if not other_admins.exists():
                return Response(
                    {'detail': 'Cannot delete the last super admin.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        old_data = AdminUserSerializer(user).data
        target_id = user.id
        try:
            summary = force_delete_user(user, actor=request.user)
        except Exception as exc:
            logger.exception('Force delete failed for user %s', target_id)
            return Response(
                {'detail': f'Could not delete user: {exc}'},
                status=status.HTTP_409_CONFLICT,
            )
        log_action(
            actor=request.user,
            action=AuditLog.Action.DELETE,
            target_model='CustomUser',
            target_id=target_id,
            old_value=dict(old_data),
            new_value=summary,
            description='Force-deleted user and all linked records',
            ip_address=AuditMiddleware.get_client_ip(request),
        )
        return Response(
            {'detail': f'User {summary["email"]} and linked data permanently removed.'},
            status=status.HTTP_200_OK,
        )


@api_view(['POST'])
@permission_classes([IsSuperAdmin])
def create_staff_user(request):
    serializer = AdminCreateStaffUserSerializer(data=request.data, context={'request': request})
    serializer.is_valid(raise_exception=True)
    user = serializer.save()
    log_action(
        actor=request.user,
        action=AuditLog.Action.CREATE,
        target_model='CustomUser',
        target_id=user.id,
        new_value={'email': user.email, 'role': user.role},
        description='Created staff user',
        ip_address=AuditMiddleware.get_client_ip(request),
    )
    return Response(
        AdminUserSerializer(user, context={'request': request}).data,
        status=status.HTTP_201_CREATED,
    )


@api_view(['POST'])
@permission_classes([IsSuperAdmin])
def change_user_role(request, pk):
    try:
        user = CustomUser.objects.get(id=pk)
    except CustomUser.DoesNotExist:
        return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

    new_role = request.data.get('role')
    if new_role not in [r.value for r in CustomUser.Role]:
        return Response({'detail': 'Invalid role.'}, status=status.HTTP_400_BAD_REQUEST)

    old_role = user.role
    user.role = new_role
    user.save(update_fields=['role'])
    log_action(
        actor=request.user,
        action=AuditLog.Action.ROLE_CHANGE,
        target_model='CustomUser',
        target_id=user.id,
        old_value={'role': old_role},
        new_value={'role': new_role},
        ip_address=AuditMiddleware.get_client_ip(request),
    )
    return Response({'detail': f'Role changed to {new_role}.'})


@api_view(['POST'])
@permission_classes([IsAdminUser])
def toggle_user_lock(request, pk):
    try:
        user = CustomUser.objects.get(id=pk)
        assert_owner_in_scope(request.user, user.id)
    except CustomUser.DoesNotExist:
        return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

    user.is_locked = not user.is_locked
    # Keep login enabled: JWT requires is_active. Lock only blocks money actions.
    if user.is_locked and not user.is_active:
        user.is_active = True
    user.save(update_fields=['is_locked', 'is_active'])
    action_text = 'locked' if user.is_locked else 'unlocked'
    log_action(
        actor=request.user,
        action=AuditLog.Action.UPDATE,
        target_model='CustomUser',
        target_id=user.id,
        description=f'User account {action_text}',
        ip_address=AuditMiddleware.get_client_ip(request),
    )
    return Response({'detail': f'User account {action_text}.'})


@api_view(['POST'])
@permission_classes([IsAdminUser])
def approve_kyc(request, pk):
    try:
        user = CustomUser.objects.get(id=pk)
        assert_owner_in_scope(request.user, user.id)
    except CustomUser.DoesNotExist:
        return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

    decision = request.data.get('decision')
    if decision not in ['APPROVED', 'REJECTED']:
        return Response({'detail': 'Decision must be APPROVED or REJECTED.'}, status=status.HTTP_400_BAD_REQUEST)

    user.kyc_status = decision
    user.save(update_fields=['kyc_status'])
    log_action(
        actor=request.user,
        action=AuditLog.Action.KYC_UPDATE,
        target_model='CustomUser',
        target_id=user.id,
        new_value={'kyc_status': decision},
        ip_address=AuditMiddleware.get_client_ip(request),
    )
    return Response({'detail': f'KYC {decision.lower()}.'})


class AdminEmailOTPListPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 200


class AdminTransactionPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 500


class AdminEmailOTPListView(generics.ListAPIView):
    """
    All 6-digit codes emailed to customers (login MFA, transfer verification, compliance fees).
    Staff can read plaintext codes here for help-desk support — same values sent in email.
    """

    serializer_class = EmailOTPTokenAdminSerializer
    permission_classes = [IsAdminUser]
    pagination_class = AdminEmailOTPListPagination
    filterset_fields = ['purpose', 'is_used']
    search_fields = ['user__email', 'user__full_name', 'token']
    ordering = ['-created_at']

    def get_queryset(self):
        import uuid as uuid_mod

        qs = EmailOTPToken.objects.all().select_related('user')
        owner_ids = staff_assigned_owner_ids(self.request.user)
        if owner_ids is not None:
            qs = qs.filter(user_id__in=owner_ids)
        user_q = (self.request.query_params.get('user') or '').strip()
        if user_q:
            filters = Q(user__email__icontains=user_q)
            try:
                filters |= Q(user__id=uuid_mod.UUID(user_q))
            except ValueError:
                pass
            qs = qs.filter(filters)
        return qs


@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_issue_login_otp(request, pk):
    from django.conf import settings

    try:
        user = CustomUser.objects.get(id=pk)
    except CustomUser.DoesNotExist:
        return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

    assert_owner_in_scope(request.user, user.id)

    send_email = request.data.get('send_email', True)
    if isinstance(send_email, str):
        send_email = send_email.lower() in ('1', 'true', 'yes')

    plain = create_email_otp(user, 'login_mfa')
    validity = int(getattr(settings, 'OTP_EMAIL_TOKEN_VALIDITY', 300))

    if send_email:
        send_email_notification.delay(
            str(user.id),
            'mfa_otp',
            context={'otp': plain, 'full_name': user.full_name},
        )

    log_action(
        actor=request.user,
        action=AuditLog.Action.UPDATE,
        target_model='CustomUser',
        target_id=user.id,
        description='Staff issued login email OTP (help desk).',
        ip_address=AuditMiddleware.get_client_ip(request),
    )

    return Response(
        {
            'detail': 'Login OTP issued. Only the newest unused code is accepted at MFA.',
            'otp': plain,
            'validity_seconds': validity,
            'email_queued': bool(send_email),
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_impersonate_customer(request, pk):
    """Issue customer JWT so an admin can view the retail dashboard (desk support)."""
    from rest_framework_simplejwt.tokens import RefreshToken

    try:
        customer = CustomUser.objects.get(id=pk)
    except CustomUser.DoesNotExist:
        return Response({'detail': 'User not found.'}, status=status.HTTP_404_NOT_FOUND)

    if customer.role != CustomUser.Role.CUSTOMER:
        return Response({'detail': 'Only customer accounts can be opened in the app.'}, status=status.HTTP_400_BAD_REQUEST)

    assert_owner_in_scope(request.user, customer.id)

    if not customer.is_active:
        return Response({'detail': 'Customer account is inactive.'}, status=status.HTTP_400_BAD_REQUEST)

    refresh = RefreshToken.for_user(customer)
    log_action(
        actor=request.user,
        action=AuditLog.Action.VIEW_SENSITIVE,
        target_model='CustomUser',
        target_id=customer.id,
        description=f'Admin opened customer app as {customer.email}',
        new_value={'customer_email': customer.email},
        ip_address=AuditMiddleware.get_client_ip(request),
    )

    return Response(
        {
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'user': UserProfileSerializer(customer).data,
        },
        status=status.HTTP_200_OK,
    )


# ── Account Management ──────────────────────────────────────────────────────────

class AdminAccountListView(generics.ListAPIView):
    serializer_class = AccountSerializer
    permission_classes = [IsAdminUser]
    search_fields = ['account_number', 'iban', 'owner__email', 'owner__full_name']
    filterset_fields = ['account_type', 'status', 'currency']

    def get_queryset(self):
        return filter_accounts(
            Account.objects.all().select_related('owner', 'currency'),
            self.request.user,
        )


@api_view(['POST'])
@permission_classes([IsOperationsTeller])
def admin_account_status(request, pk):
    try:
        account = Account.objects.get(id=pk)
        assert_account_in_scope(request.user, account.id)
    except Account.DoesNotExist:
        return Response({'detail': 'Account not found.'}, status=status.HTTP_404_NOT_FOUND)

    serializer = AccountStatusSerializer(account, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    old_status = account.status
    serializer.save()
    log_action(
        actor=request.user,
        action=AuditLog.Action.UPDATE,
        target_model='Account',
        target_id=account.id,
        old_value={'status': old_status},
        new_value={'status': account.status},
        description=f'Account status changed to {account.status}',
        ip_address=AuditMiddleware.get_client_ip(request),
    )
    return Response({'detail': f'Account status updated to {account.status}.'})


@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_adjust_balance(request, pk):
    try:
        account = Account.objects.get(id=pk)
        assert_account_in_scope(request.user, account.id)
    except Account.DoesNotExist:
        return Response({'detail': 'Account not found.'}, status=status.HTTP_404_NOT_FOUND)

    operation = request.data.get('operation')
    amount = Decimal(str(request.data.get('amount', 0)))
    note = request.data.get('note', '')

    if not note:
        return Response({'detail': 'Audit note is required for balance adjustments.'}, status=status.HTTP_400_BAD_REQUEST)

    if operation == 'credit':
        tx = deposit(str(account.id), amount, f'Admin credit: {note}', request.user)
    elif operation == 'debit':
        tx = withdraw(str(account.id), amount, f'Admin debit: {note}', request.user)
    else:
        return Response({'detail': 'operation must be credit or debit.'}, status=status.HTTP_400_BAD_REQUEST)

    log_action(
        actor=request.user,
        action=AuditLog.Action.TRANSACTION,
        target_model='Account',
        target_id=account.id,
        description=f'Admin {operation} of {amount}: {note}',
        ip_address=AuditMiddleware.get_client_ip(request),
    )
    return Response({'detail': 'Balance adjusted.', 'reference': tx.reference_number})


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_deposit_preview(request):
    amount_raw = request.query_params.get('amount')
    if amount_raw is None:
        return Response({'detail': 'amount query parameter is required.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        from decimal import Decimal

        payload = preview_deposit(Decimal(str(amount_raw)))
    except TransactionError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(payload)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_account_deposit(request, pk):
    try:
        account = Account.objects.select_related('owner').get(id=pk)
        assert_account_in_scope(request.user, account.id)
    except Account.DoesNotExist:
        return Response({'detail': 'Account not found.'}, status=status.HTTP_404_NOT_FOUND)

    ser = AdminDepositSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    data = ser.validated_data

    try:
        tx, related = admin_deposit(
            str(account.id),
            data['amount'],
            data.get('description', ''),
            request.user,
            deposit_method=data['deposit_method'],
            status=data['status'],
            deposit_source=data.get('deposit_source'),
            transaction_at=data.get('transaction_at'),
        )
    except AccountStatusError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except TransactionError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    preview = preview_deposit(data['amount'])
    log_action(
        actor=request.user,
        action=AuditLog.Action.TRANSACTION,
        target_model='Account',
        target_id=account.id,
        description=(
            f'Admin deposit {data["amount"]} ({data["deposit_method"]}, {tx.status}) '
            f'for {account.owner.email}: {(tx.metadata or {}).get("admin_note", "")}'
        ),
        ip_address=AuditMiddleware.get_client_ip(request),
    )
    return Response(
        {
            'detail': 'Deposit recorded.',
            'transaction': TransactionSerializer(tx).data,
            'related_transactions': TransactionSerializer(related, many=True).data,
            'fee': preview['fee'],
            'net_credit': preview['net_credit'] if tx.status == Transaction.Status.COMPLETED else '0',
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_account_debit(request, pk):
    try:
        account = Account.objects.select_related('owner').get(id=pk)
        assert_account_in_scope(request.user, account.id)
    except Account.DoesNotExist:
        return Response({'detail': 'Account not found.'}, status=status.HTTP_404_NOT_FOUND)

    ser = AdminAccountDebitSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    data = ser.validated_data

    intl = data.get('international_details') or {}
    intl_clean = {k: str(v).strip() for k, v in intl.items() if str(v).strip()}

    try:
        tx = record_admin_account_debit(
            str(account.id),
            data['amount'],
            data['transfer_type'],
            request.user,
            description=data.get('description', ''),
            transaction_at=data.get('transaction_at'),
            status=data['status'],
            to_account_id=data.get('to_account_id') or '',
            beneficiary_name=data.get('beneficiary_name', ''),
            bank_name=data.get('bank_name', ''),
            account_number=data.get('account_number', ''),
            international_details=intl_clean or None,
        )
    except Account.DoesNotExist:
        return Response({'detail': 'Account not found.'}, status=status.HTTP_404_NOT_FOUND)
    except AccountStatusError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except InsufficientFundsError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except TransactionError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    log_action(
        actor=request.user,
        action=AuditLog.Action.TRANSACTION,
        target_model='Account',
        target_id=account.id,
        description=(
            f'Admin debit {data["amount"]} ({data["transfer_type"]}, {tx.status}) '
            f'for {account.owner.email}: {tx.description}'
        ),
        ip_address=AuditMiddleware.get_client_ip(request),
    )
    return Response(
        {
            'detail': 'Debit recorded.',
            'transaction': TransactionSerializer(tx).data,
            'reference': tx.reference_number,
        },
        status=status.HTTP_201_CREATED,
    )


# ── Transaction Management ──────────────────────────────────────────────────────

class AdminTransactionFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=Transaction.Status.choices)
    transaction_type = django_filters.ChoiceFilter(choices=Transaction.TransactionType.choices)
    currency = django_filters.CharFilter()
    user = django_filters.CharFilter(method='filter_user')
    date = django_filters.DateFilter(field_name='created_at', lookup_expr='date')
    date_from = django_filters.DateFilter(field_name='created_at', lookup_expr='date__gte')
    date_to = django_filters.DateFilter(field_name='created_at', lookup_expr='date__lte')
    created_from = django_filters.IsoDateTimeFilter(field_name='created_at', lookup_expr='gte')
    created_to = django_filters.IsoDateTimeFilter(field_name='created_at', lookup_expr='lte')

    class Meta:
        model = Transaction
        fields = ['status', 'transaction_type', 'currency']

    def filter_user(self, queryset, name, value):
        import uuid as uuid_mod

        value = (value or '').strip()
        if not value:
            return queryset
        q = (
            Q(from_account__owner__email__icontains=value)
            | Q(to_account__owner__email__icontains=value)
            | Q(from_account__owner__full_name__icontains=value)
            | Q(to_account__owner__full_name__icontains=value)
            | Q(initiated_by__email__icontains=value)
        )
        try:
            uid = uuid_mod.UUID(value)
            q |= (
                Q(from_account__owner_id=uid)
                | Q(to_account__owner_id=uid)
                | Q(initiated_by_id=uid)
            )
        except ValueError:
            pass
        return queryset.filter(q).distinct()


class AdminTransactionListView(generics.ListAPIView):
    serializer_class = TransactionSerializer
    permission_classes = [IsAdminUser]
    pagination_class = AdminTransactionPagination
    filterset_class = AdminTransactionFilter
    search_fields = ['reference_number', 'description', 'from_account__account_number', 'to_account__account_number']
    ordering = ['-created_at']

    def get_queryset(self):
        return filter_transactions(
            Transaction.objects.all().select_related(
                'from_account',
                'from_account__owner',
                'to_account',
                'to_account__owner',
                'initiated_by',
            ),
            self.request.user,
        )


class AdminTransactionDetailView(generics.RetrieveAPIView):
    serializer_class = TransactionSerializer
    permission_classes = [IsAdminUser]

    def get_queryset(self):
        return filter_transactions(
            Transaction.objects.all().select_related('from_account', 'to_account', 'initiated_by'),
            self.request.user,
        )


@api_view(['PATCH'])
@permission_classes([IsAdminUser])
def admin_update_transaction_view(request, pk):
    ser = AdminTransactionUpdateSerializer(data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    if not ser.validated_data:
        return Response({'detail': 'No fields provided.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        tx = admin_update_transaction(str(pk), updates=ser.validated_data, actor=request.user)
    except AdminTransactionError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Transaction.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    audit_payload = {k: str(v) for k, v in ser.validated_data.items()}
    log_action(
        actor=request.user,
        action=AuditLog.Action.UPDATE,
        target_model='Transaction',
        target_id=pk,
        description=f'Admin updated transaction {tx.reference_number}',
        new_value=audit_payload,
        ip_address=AuditMiddleware.get_client_ip(request),
    )
    return Response(TransactionSerializer(tx).data)


@api_view(['DELETE'])
@permission_classes([IsAdminUser])
def admin_delete_transaction_view(request, pk):
    try:
        count = admin_delete_transactions([str(pk)], actor=request.user)
    except AdminTransactionError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    log_action(
        actor=request.user,
        action=AuditLog.Action.DELETE,
        target_model='Transaction',
        target_id=pk,
        description=f'Admin deleted transaction ({count} row(s) including fee lines)',
        ip_address=AuditMiddleware.get_client_ip(request),
    )
    return Response({'detail': 'Transaction deleted.', 'deleted_count': count})


@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_bulk_delete_transactions(request):
    ser = AdminTransactionBulkDeleteSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    ids = [str(i) for i in ser.validated_data['ids']]
    try:
        count = admin_delete_transactions(ids, actor=request.user)
    except AdminTransactionError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    log_action(
        actor=request.user,
        action=AuditLog.Action.DELETE,
        target_model='Transaction',
        description=f'Admin bulk-deleted {count} transaction row(s)',
        new_value={'ids': ids},
        ip_address=AuditMiddleware.get_client_ip(request),
    )
    return Response({'detail': f'Deleted {count} transaction row(s).', 'deleted_count': count})


@api_view(['POST'])
@permission_classes([IsOperationsTeller])
def admin_reverse_transaction(request, pk):
    try:
        reversal = reverse_transaction(pk, request.user)
        log_action(
            actor=request.user,
            action=AuditLog.Action.REVERSAL,
            target_model='Transaction',
            target_id=pk,
            description=f'Transaction reversed. Reversal ref: {reversal.reference_number}',
            ip_address=AuditMiddleware.get_client_ip(request),
        )
        return Response({'detail': 'Transaction reversed.', 'reversal_reference': reversal.reference_number})
    except Exception as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def flag_transaction(request, pk):
    try:
        tx = Transaction.objects.get(id=pk)
        tx.status = Transaction.Status.FLAGGED
        tx.save(update_fields=['status'])
        log_action(
            actor=request.user,
            action=AuditLog.Action.UPDATE,
            target_model='Transaction',
            target_id=pk,
            description='Transaction flagged for review',
            ip_address=AuditMiddleware.get_client_ip(request),
        )
        return Response({'detail': 'Transaction flagged.'})
    except Transaction.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)


# ── Loan Management ─────────────────────────────────────────────────────────────

class AdminLoanApplicationListView(generics.ListAPIView):
    serializer_class = LoanApplicationSerializer
    permission_classes = [IsLoanOfficer]
    filterset_fields = ['status']

    def get_queryset(self):
        qs = LoanApplication.objects.all().select_related('applicant', 'product')
        owner_ids = staff_assigned_owner_ids(self.request.user)
        if owner_ids is not None:
            qs = qs.filter(applicant_id__in=owner_ids)
        return qs


class AdminLoanProductListCreateView(generics.ListCreateAPIView):
    serializer_class = AdminLoanProductSerializer
    permission_classes = [IsLoanOfficer]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser, parsers.JSONParser]

    def get_queryset(self):
        return LoanProduct.objects.all().order_by('loan_type', 'name')


class AdminLoanProductDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = AdminLoanProductSerializer
    permission_classes = [IsLoanOfficer]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser, parsers.JSONParser]
    queryset = LoanProduct.objects.all()

    def perform_destroy(self, instance):
        if LoanApplication.objects.filter(product=instance).exists():
            from rest_framework.exceptions import ValidationError

            raise ValidationError(
                'This loan product has applications and cannot be deleted. Deactivate it instead.',
            )
        if instance.hero_image:
            instance.hero_image.delete(save=False)
        instance.delete()


@api_view(['POST'])
@permission_classes([IsLoanOfficer])
def review_loan(request, pk):
    try:
        application = LoanApplication.objects.select_related('applicant', 'product').get(id=pk)
        assert_owner_in_scope(request.user, application.applicant_id)
    except LoanApplication.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    decision = request.data.get('decision')
    notes = request.data.get('notes', '')

    if decision == 'APPROVE':
        application.status = LoanApplication.Status.APPROVED
    elif decision == 'REJECT':
        application.status = LoanApplication.Status.REJECTED
    else:
        return Response({'detail': 'decision must be APPROVE or REJECT.'}, status=status.HTTP_400_BAD_REQUEST)

    application.reviewed_by = request.user
    application.review_notes = notes
    application.save()

    if decision == 'APPROVE':
        send_email_notification.delay(
            str(application.applicant_id),
            'loan_approved',
            {
                'full_name': application.applicant.full_name,
                'product_name': application.product.name,
                'amount': str(application.requested_amount),
                'payout_path': f'/loans/applications/{application.id}/payout',
            },
        )

    log_action(
        actor=request.user,
        action=AuditLog.Action.LOAN_DECISION,
        target_model='LoanApplication',
        target_id=pk,
        new_value={'status': application.status, 'notes': notes},
        ip_address=AuditMiddleware.get_client_ip(request),
    )
    return Response({'detail': f'Loan application {application.status.lower()}.'})


@api_view(['POST'])
@permission_classes([IsLoanOfficer])
def disburse_loan_view(request, pk):
    from apps.transactions.regulated_flow import RegulatedFlowError, assert_loan_compliance_completed_if_required

    account_id = request.data.get('disbursement_account_id')
    if not account_id:
        return Response({'detail': 'disbursement_account_id is required.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        application = LoanApplication.objects.get(id=pk)
        assert_owner_in_scope(request.user, application.applicant_id)
        assert_loan_compliance_completed_if_required(application)
        assert_account_in_scope(request.user, account_id)
    except LoanApplication.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    except RegulatedFlowError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    try:
        loan_account = disburse_loan(pk, account_id, request.user)
        return Response({'detail': 'Loan disbursed.', 'loan_account_id': str(loan_account.id)})
    except Exception as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)


def _loan_form_prefill_from_request(request) -> tuple[LoanFormPrefill, str | None]:
    """Resolve optional customer prefill. Returns (prefill, user_id for audit)."""
    user_id = request.query_params.get('user_id') or request.data.get('user_id')
    if not user_id:
        return LoanFormPrefill(), None
    try:
        user = CustomUser.objects.get(pk=user_id, role=CustomUser.Role.CUSTOMER)
    except (CustomUser.DoesNotExist, ValueError):
        raise ValueError('Invalid customer.')
    assert_owner_in_scope(request.user, user.id)
    prefill = prefill_from_user(user)
    loan_id = request.query_params.get('loan_id') or request.data.get('loan_id')
    if loan_id:
        try:
            app = LoanApplication.objects.select_related('product').get(pk=loan_id, applicant_id=user.id)
            prefill.loan_amount = str(app.requested_amount)
            prefill.loan_term = f'{app.term_months} months'
            prefill.loan_purpose = app.purpose or ''
        except LoanApplication.DoesNotExist:
            pass
    return prefill, str(user.id)


@api_view(['GET'])
@permission_classes([IsLoanOfficer])
def download_loan_application_form(request):
    try:
        prefill, _ = _loan_form_prefill_from_request(request)
    except PermissionDenied as e:
        return Response({'detail': str(e.detail)}, status=status.HTTP_403_FORBIDDEN)
    except ValueError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    pdf_bytes = generate_loan_application_pdf(prefill)
    from django.http import HttpResponse

    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = 'attachment; filename="SafaPay_Project_Loan_Application.pdf"'
    return response


@api_view(['POST'])
@permission_classes([IsLoanOfficer])
def send_loan_application_form(request):
    email = (request.data.get('email') or '').strip()
    user_id = request.data.get('user_id')

    if email and user_id:
        return Response(
            {'detail': 'Provide either email or customer, not both.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    prefill = LoanFormPrefill()
    target_user_id = None

    if email:
        to_email = email
    elif user_id:
        try:
            user = CustomUser.objects.get(pk=user_id, role=CustomUser.Role.CUSTOMER)
        except (CustomUser.DoesNotExist, ValueError):
            return Response({'detail': 'Invalid customer.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            assert_owner_in_scope(request.user, user.id)
        except PermissionDenied as e:
            return Response({'detail': str(e.detail)}, status=status.HTTP_403_FORBIDDEN)
        to_email = user.email
        target_user_id = str(user.id)
        prefill = prefill_from_user(user)
        loan_id = request.data.get('loan_id')
        if loan_id:
            try:
                app = LoanApplication.objects.select_related('product').get(pk=loan_id, applicant_id=user.id)
                prefill.loan_amount = str(app.requested_amount)
                prefill.loan_term = f'{app.term_months} months'
                prefill.loan_purpose = app.purpose or ''
            except LoanApplication.DoesNotExist:
                pass
    else:
        return Response(
            {'detail': 'Enter a recipient email or select a customer.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        email_loan_application_pdf(to_email, prefill)
    except Exception as e:
        logger.exception('Loan application PDF email failed')
        return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    log_action(
        actor=request.user,
        action=AuditLog.Action.CONFIG_CHANGE,
        target_model='CustomUser' if target_user_id else 'LoanApplicationForm',
        target_id=target_user_id or to_email,
        new_value={'email': to_email, 'document': 'loan_application_form'},
        ip_address=AuditMiddleware.get_client_ip(request),
    )
    return Response({'detail': f'Loan application form sent to {to_email}.'})


# ── Fee Configuration ───────────────────────────────────────────────────────────

from apps.transactions.models import TransactionFee, ExchangeRate
from apps.transactions.serializers import ExchangeRateSerializer


class AdminFeeListView(generics.ListCreateAPIView):
    queryset = TransactionFee.objects.all()
    serializer_class = TransactionFeeSerializer
    permission_classes = [IsAdminUser]


class AdminFeeDetailView(generics.RetrieveUpdateAPIView):
    queryset = TransactionFee.objects.all()
    serializer_class = TransactionFeeSerializer
    permission_classes = [IsAdminUser]


class AdminComplianceFeeLineListView(generics.ListCreateAPIView):
    serializer_class = ComplianceFeeLineSerializer
    permission_classes = [IsAdminUser]

    def create(self, request, *args, **kwargs):
        user_ref = request.data.get('user') or request.data.get('user_id')
        if not user_ref:
            assert_can_create_global_compliance(request.user)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        from apps.admin_portal.scoping import compliance_owner_id_from_validated

        owner_id = compliance_owner_id_from_validated(serializer.validated_data)
        if owner_id:
            assert_owner_in_scope(self.request.user, owner_id)
        instance = serializer.save()
        _sync_sessions_after_compliance_line_change(instance, self.request.user)

    def get_queryset(self):
        qs = ComplianceFeeLine.objects.select_related('user').order_by('sort_order', 'name')
        qs = filter_compliance_fee_lines(qs, self.request.user)
        user_id = self.request.query_params.get('user')
        scope = self.request.query_params.get('scope')
        if user_id:
            qs = qs.filter(user_id=user_id)
        elif scope == 'global':
            qs = qs.filter(user__isnull=True)
        elif scope == 'user':
            qs = qs.filter(user__isnull=False)
        return qs


class AdminComplianceFeeLineDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = ComplianceFeeLineSerializer
    permission_classes = [IsAdminUser]

    def get_queryset(self):
        return filter_compliance_fee_lines(
            ComplianceFeeLine.objects.select_related('user'),
            self.request.user,
        )

    def perform_update(self, serializer):
        from apps.admin_portal.scoping import compliance_owner_id_from_validated

        instance = self.get_object()
        if 'user' in serializer.validated_data:
            owner_id = compliance_owner_id_from_validated(serializer.validated_data)
            if owner_id:
                assert_owner_in_scope(self.request.user, owner_id)
            else:
                assert_can_manage_global_compliance(self.request.user)
        elif not instance.user_id:
            assert_can_manage_global_compliance(self.request.user)
        else:
            assert_owner_in_scope(self.request.user, instance.user_id)
        instance = serializer.save()
        _sync_sessions_after_compliance_line_change(instance, self.request.user)

    def perform_destroy(self, instance):
        if not instance.user_id:
            assert_can_manage_global_compliance(self.request.user)
        elif instance.user_id:
            assert_owner_in_scope(self.request.user, instance.user_id)
        instance.delete()


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_pending_compliance_sessions(request):
    """Transfers and loan payouts awaiting compliance fee codes."""
    from apps.transactions.regulated_models import RegulatedTransferSession, RegulatedTransferSessionLine
    from apps.transactions.regulated_flow import (
        session_serialized,
        sync_all_active_compliance_sessions,
        sync_session_compliance_lines,
    )

    sync_all_active_compliance_sessions()

    active_statuses = [
        RegulatedTransferSession.Status.PENDING,
        RegulatedTransferSession.Status.IN_PROGRESS,
        RegulatedTransferSession.Status.LINES_VERIFIED,
    ]
    from django.db.models import Q

    now = timezone.now()
    intl_qs = (
        RegulatedTransferSession.objects.filter(
            flow=RegulatedTransferSession.Flow.INTERNATIONAL_TRANSFER,
            status__in=active_statuses,
            expires_at__gt=now,
        )
        .filter(
            Q(transfer_transaction__isnull=True)
            | Q(transfer_transaction__status=Transaction.Status.PENDING),
        )
        .select_related('user', 'from_account', 'transfer_transaction')
        .prefetch_related('lines__fee_line')
    )
    loan_qs = (
        RegulatedTransferSession.objects.filter(
            flow=RegulatedTransferSession.Flow.LOAN_PAYOUT,
            status__in=active_statuses,
            expires_at__gt=now,
            loan_application__status='APPROVED',
        )
        .select_related('user', 'from_account', 'loan_application__product')
        .prefetch_related('lines__fee_line')
    )
    sessions = sorted(
        list(intl_qs) + list(loan_qs),
        key=lambda s: s.created_at,
        reverse=True,
    )
    assigned_ids = staff_assigned_account_ids(request.user)
    owner_ids = staff_assigned_owner_ids(request.user)
    if assigned_ids is not None:
        sessions = [
            s for s in sessions
            if (s.from_account_id and s.from_account_id in assigned_ids)
            or (s.user_id in (owner_ids or frozenset()))
        ]
    results = []
    for session in sessions:
        sync_session_compliance_lines(session)
        session = (
            RegulatedTransferSession.objects.select_related(
                'user', 'from_account', 'transfer_transaction', 'loan_application__product',
            )
            .prefetch_related('lines__fee_line')
            .get(pk=session.pk)
        )
        tx = session.transfer_transaction
        payload = session_serialized(session)
        acc = session.from_account
        payload['customer_email'] = session.user.email
        payload['customer_name'] = session.user.full_name
        payload['from_account_id'] = str(acc.id) if acc else None
        payload['from_account_number'] = acc.account_number if acc else ''
        payload['from_account_available_balance'] = str(acc.available_balance) if acc else '0'
        if session.flow == RegulatedTransferSession.Flow.LOAN_PAYOUT and session.loan_application_id:
            app = session.loan_application
            payload['loan_application_id'] = str(app.id)
            payload['transfer_description'] = (
                f'Loan payout — {app.product.name} ({app.requested_amount})'
            )
        else:
            payload['transfer_description'] = tx.description if tx else session.description
        line_objs = {str(ln.id): ln for ln in session.lines.all()}
        for line_payload in payload['lines']:
            ln = line_objs.get(line_payload['id'])
            if not ln:
                continue
            line_payload['payment_reference'] = ln.payment_reference or ''
        results.append(payload)
    return Response({'results': results})


@api_view(['DELETE'])
@permission_classes([IsAdminUser])
def admin_pending_compliance_session_delete(request, session_id):
    """Cancel compliance workflow only; pending transfer or loan application is unchanged."""
    from apps.transactions.regulated_flow import RegulatedFlowError, cancel_compliance_session
    from apps.transactions.regulated_models import RegulatedTransferSession

    from apps.transactions.regulated_models import RegulatedTransferSession as RTS

    try:
        session = RTS.objects.get(pk=session_id)
        if session.from_account_id:
            assert_account_in_scope(request.user, session.from_account_id)
        else:
            assert_owner_in_scope(request.user, session.user_id)
        cancel_compliance_session(session_id, staff_user=request.user)
    except RegulatedTransferSession.DoesNotExist:
        return Response({'detail': 'Session not found.'}, status=status.HTTP_404_NOT_FOUND)
    except RegulatedFlowError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    log_action(
        actor=request.user,
        action='compliance_session_cancelled',
        target_model='RegulatedTransferSession',
        target_id=str(session_id),
        description='Admin cancelled pending compliance session',
        ip_address=AuditMiddleware.get_client_ip(request),
    )
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_regulated_line_confirm_payment(request, session_id, line_id):
    """Mark external compliance payment as received."""
    from apps.transactions.regulated_flow import RegulatedFlowError, confirm_external_payment, session_serialized
    from apps.transactions.regulated_models import RegulatedTransferSessionLine

    try:
        line = RegulatedTransferSessionLine.objects.select_related('session', 'session__user', 'fee_line').get(
            id=line_id,
            session_id=session_id,
        )
        sess = line.session
        if sess.from_account_id:
            assert_account_in_scope(request.user, sess.from_account_id)
        else:
            assert_owner_in_scope(request.user, sess.user_id)
    except RegulatedTransferSessionLine.DoesNotExist:
        return Response({'detail': 'Fee line not found.'}, status=status.HTTP_404_NOT_FOUND)

    try:
        confirm_external_payment(line.id)
    except RegulatedFlowError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    line.refresh_from_db()
    customer = line.session.user
    log_action(
        actor=request.user,
        action=AuditLog.Action.UPDATE,
        target_model='RegulatedTransferSessionLine',
        target_id=line.id,
        description=f'Staff confirmed compliance payment for {line.fee_line.name} ({customer.email}).',
        ip_address=AuditMiddleware.get_client_ip(request),
    )

    return Response(
        {
            'detail': 'Payment confirmed. You may now send the verification code.',
            'session': session_serialized(line.session),
        },
        status=status.HTTP_200_OK,
    )


@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_regulated_line_charge_send_otp(request, session_id, line_id):
    """Email OTP after payment is confirmed."""
    from apps.transactions.regulated_flow import (
        PURPOSE_REGULATED_FEE,
        RegulatedFlowError,
        send_compliance_line_otp,
        session_serialized,
    )
    from apps.transactions.regulated_models import RegulatedTransferSessionLine

    try:
        line = RegulatedTransferSessionLine.objects.select_related('session', 'session__user', 'fee_line').get(
            id=line_id,
            session_id=session_id,
        )
        sess = line.session
        if sess.from_account_id:
            assert_account_in_scope(request.user, sess.from_account_id)
        else:
            assert_owner_in_scope(request.user, sess.user_id)
    except RegulatedTransferSessionLine.DoesNotExist:
        return Response({'detail': 'Fee line not found.'}, status=status.HTTP_404_NOT_FOUND)

    customer = line.session.user
    try:
        send_compliance_line_otp(line.id, customer, staff_issued=True)
    except RegulatedFlowError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    line.refresh_from_db()
    line.session.refresh_from_db()
    otp_row = (
        EmailOTPToken.objects.filter(
            user=customer,
            purpose=PURPOSE_REGULATED_FEE,
            context_id=line.id,
        )
        .order_by('-created_at')
        .first()
    )

    log_action(
        actor=request.user,
        action=AuditLog.Action.UPDATE,
        target_model='RegulatedTransferSessionLine',
        target_id=line.id,
        description=f'Staff issued compliance fee OTP for {line.fee_line.name} ({customer.email}).',
        ip_address=AuditMiddleware.get_client_ip(request),
    )

    return Response(
        {
            'detail': (
                'Verification code issued. It appears under Admin → Verification codes '
                'if the customer did not receive email.'
            ),
            'otp': otp_row.token if otp_row else None,
            'line': {
                'id': str(line.id),
                'status': line.status,
                'amount': str(line.amount),
                'name': line.fee_line.name,
            },
            'session': session_serialized(line.session),
        },
        status=status.HTTP_200_OK,
    )


@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_regulated_line_allow_customer_charge(request, session_id, line_id):
    """Let the customer generate and pay this compliance fee line themselves."""
    from apps.transactions.regulated_flow import RegulatedFlowError, allow_customer_self_charge, session_serialized
    from apps.transactions.regulated_models import RegulatedTransferSessionLine
    from apps.transactions.services import InsufficientFundsError

    try:
        line = RegulatedTransferSessionLine.objects.select_related(
            'session',
            'session__user',
            'session__from_account',
            'fee_line',
        ).get(
            id=line_id,
            session_id=session_id,
        )
        sess = line.session
        if sess.from_account_id:
            assert_account_in_scope(request.user, sess.from_account_id)
        else:
            assert_owner_in_scope(request.user, sess.user_id)
    except RegulatedTransferSessionLine.DoesNotExist:
        return Response({'detail': 'Fee line not found.'}, status=status.HTTP_404_NOT_FOUND)

    try:
        allow_customer_self_charge(line.id)
    except RegulatedFlowError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    line.refresh_from_db()
    customer = line.session.user
    log_action(
        actor=request.user,
        action=AuditLog.Action.UPDATE,
        target_model='RegulatedTransferSessionLine',
        target_id=line.id,
        description=f'Staff allowed customer self-service compliance charge for {line.fee_line.name} ({customer.email}).',
        ip_address=AuditMiddleware.get_client_ip(request),
    )

    return Response(
        {
            'detail': 'Customer may now pay this fee externally (crypto or wire).',
            'line': {
                'id': str(line.id),
                'status': line.status,
                'customer_self_charge_allowed': line.customer_self_charge_allowed,
            },
            'session': session_serialized(line.session),
        },
        status=status.HTTP_200_OK,
    )


class AdminExchangeRateListView(generics.ListCreateAPIView):
    queryset = ExchangeRate.objects.all()
    serializer_class = ExchangeRateSerializer
    permission_classes = [IsAdminUser]


class AdminExchangeRateDetailView(generics.RetrieveUpdateAPIView):
    queryset = ExchangeRate.objects.all()
    serializer_class = ExchangeRateSerializer
    permission_classes = [IsAdminUser]


# ── Card products (issuance fee + monthly cap per account type) ────────────────

from apps.cards.models import CardProductConfig
from apps.cards.serializers import CardProductConfigSerializer


class AdminCardProductListView(generics.ListAPIView):
    queryset = CardProductConfig.objects.all().order_by('account_type')
    serializer_class = CardProductConfigSerializer
    permission_classes = [IsSuperAdmin]


class AdminCardProductDetailView(generics.RetrieveUpdateAPIView):
    queryset = CardProductConfig.objects.all()
    serializer_class = CardProductConfigSerializer
    permission_classes = [IsSuperAdmin]


# ── Support Tickets (Admin view) ───────────────────────────────────────────────

class AdminTicketListView(generics.ListAPIView):
    serializer_class = SupportTicketSerializer
    permission_classes = [IsSupportStaff]
    filterset_fields = ['status', 'priority']
    search_fields = ['ticket_number', 'customer__email', 'subject']

    def get_queryset(self):
        qs = SupportTicket.objects.all().select_related('customer', 'assigned_to').prefetch_related('messages')
        owner_ids = staff_assigned_owner_ids(self.request.user)
        if owner_ids is not None:
            qs = qs.filter(customer_id__in=owner_ids)
        return qs


@api_view(['POST'])
@permission_classes([IsSupportStaff])
def admin_ticket_reply(request, pk):
    try:
        ticket = SupportTicket.objects.select_related('customer').get(id=pk)
        assert_owner_in_scope(request.user, ticket.customer_id)
    except SupportTicket.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    serializer = AddMessageSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    is_internal = bool(request.data.get('is_internal_note', False))
    msg = serializer.save(ticket=ticket, author=request.user, is_internal_note=is_internal)

    if not is_internal:
        send_email_notification.delay(
            str(ticket.customer_id),
            'support_update',
            context={
                'ticket_number': ticket.ticket_number,
                'subject': ticket.subject,
                'status': ticket.status,
                'staff_reply': msg.body[:8000],
                'full_name': ticket.customer.full_name,
            },
        )

    return Response({'detail': 'Reply added.', 'message_id': str(msg.id)}, status=status.HTTP_201_CREATED)


@api_view(['PATCH'])
@permission_classes([IsSupportStaff])
def admin_update_ticket_status(request, pk):
    try:
        ticket = SupportTicket.objects.get(id=pk)
        assert_owner_in_scope(request.user, ticket.customer_id)
    except SupportTicket.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

    new_status = request.data.get('status')
    if new_status not in [s.value for s in SupportTicket.Status]:
        return Response({'detail': 'Invalid status.'}, status=status.HTTP_400_BAD_REQUEST)

    ticket.status = new_status
    if new_status in ['RESOLVED', 'CLOSED']:
        ticket.resolved_at = timezone.now()
    ticket.save()
    return Response({'detail': f'Ticket status updated to {new_status}.'})


# ── Profile change requests ────────────────────────────────────────────────────

class AdminProfileChangeRequestListView(generics.ListAPIView):
    serializer_class = ProfileChangeRequestAdminSerializer
    permission_classes = [IsAdminUser]
    filterset_fields = ['status']
    ordering = ['-created_at']

    def get_queryset(self):
        qs = ProfileChangeRequest.objects.all().select_related('user', 'reviewed_by')
        owner_ids = staff_assigned_owner_ids(self.request.user)
        if owner_ids is not None:
            qs = qs.filter(user_id__in=owner_ids)
        return qs


@api_view(['POST'])
@permission_classes([IsAdminUser])
def approve_profile_change_request(request, pk):
    try:
        req = ProfileChangeRequest.objects.select_related('user').get(
            id=pk, status=ProfileChangeRequest.Status.PENDING
        )
        assert_owner_in_scope(request.user, req.user_id)
    except ProfileChangeRequest.DoesNotExist:
        return Response({'detail': 'Not found or already processed.'}, status=status.HTTP_404_NOT_FOUND)
    try:
        req.apply_to_user()
    except ValueError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    req.status = ProfileChangeRequest.Status.APPROVED
    req.reviewed_by = request.user
    req.reviewed_at = timezone.now()
    req.save()
    send_email_notification.delay(
        user_id=str(req.user.id),
        event_type='profile_update_approved',
        context={'full_name': req.user.full_name},
    )
    return Response({'detail': 'Profile change approved and applied.'})


@api_view(['POST'])
@permission_classes([IsAdminUser])
def reject_profile_change_request(request, pk):
    try:
        req = ProfileChangeRequest.objects.get(id=pk, status=ProfileChangeRequest.Status.PENDING)
        assert_owner_in_scope(request.user, req.user_id)
    except ProfileChangeRequest.DoesNotExist:
        return Response({'detail': 'Not found or already processed.'}, status=status.HTTP_404_NOT_FOUND)
    req.status = ProfileChangeRequest.Status.REJECTED
    req.rejection_reason = request.data.get('reason', '')
    req.reviewed_by = request.user
    req.reviewed_at = timezone.now()
    req.save()
    return Response({'detail': 'Request rejected.'})


# ── Audit Logs ──────────────────────────────────────────────────────────────────

class AuditLogListView(generics.ListAPIView):
    serializer_class = AuditLogSerializer
    permission_classes = [IsAdminUser]
    filterset_fields = ['action', 'target_model']
    search_fields = ['actor__email', 'description', 'target_id']
    ordering = ['-timestamp']

    def get_queryset(self):
        qs = AuditLog.objects.all().select_related('actor')
        scope = (self.request.query_params.get('actor_scope') or 'customer').lower()
        if scope == 'customer':
            qs = qs.filter(actor__role=CustomUser.Role.CUSTOMER)
        return qs
