from django.db.models import Q
from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from apps.users.permissions import CustomerAccessPermission
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
import django_filters

from .models import Transaction, TransactionFee, ExchangeRate
from apps.accounts.models import Account
from .serializers import (
    TransactionSerializer,
    DepositSerializer,
    WithdrawSerializer,
    TransferSerializer,
    TransferPreviewSerializer,
    TransferSendOtpSerializer,
    TransactionFeeSerializer,
    ExchangeRateSerializer,
)
from .services import (
    deposit,
    withdraw,
    transfer,
    complete_pending_international_transfer,
    record_outbound_transfer,
    InsufficientFundsError,
    AccountStatusError,
    TransactionError,
    preview_transfer_fees,
    preview_transfer_fees_for_account_number,
    build_transfer_recipient_metadata,
)
from .regulated_flow import (
    RegulatedFlowError,
    assert_session_ready_for_international_transfer,
    mark_international_session_completed,
)
from apps.users.models import EmailOTPToken
from apps.users.email_otp import create_email_otp, invalidate_unused_email_otps
from apps.notifications.services import queue_email_notification, send_transaction_notification


class TransactionListPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 100


class TransactionFilter(django_filters.FilterSet):
    start_date = django_filters.DateTimeFilter(field_name='created_at', lookup_expr='gte')
    end_date = django_filters.DateTimeFilter(field_name='created_at', lookup_expr='lte')
    min_amount = django_filters.NumberFilter(field_name='amount', lookup_expr='gte')
    max_amount = django_filters.NumberFilter(field_name='amount', lookup_expr='lte')

    class Meta:
        model = Transaction
        fields = ['transaction_type', 'status', 'currency', 'start_date', 'end_date', 'min_amount', 'max_amount']


class TransactionListView(generics.ListAPIView):
    serializer_class = TransactionSerializer
    permission_classes = [CustomerAccessPermission]
    pagination_class = TransactionListPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_class = TransactionFilter
    search_fields = ['reference_number', 'description']
    ordering_fields = ['created_at', 'amount']
    ordering = ['-created_at']

    def get_queryset(self):
        user = self.request.user
        user_accounts = Account.objects.filter(owner=user).values_list('id', flat=True)
        return Transaction.objects.filter(
            Q(from_account_id__in=user_accounts) | Q(to_account_id__in=user_accounts)
        ).select_related('from_account', 'to_account', 'initiated_by')


class TransactionDetailView(generics.RetrieveAPIView):
    serializer_class = TransactionSerializer
    permission_classes = [CustomerAccessPermission]

    def get_queryset(self):
        user = self.request.user
        user_accounts = Account.objects.filter(owner=user).values_list('id', flat=True)
        return Transaction.objects.filter(
            Q(from_account_id__in=user_accounts) | Q(to_account_id__in=user_accounts)
        )


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def deposit_view(request):
    serializer = DepositSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    try:
        account = Account.objects.get(id=data['account_id'], owner=request.user)
    except Account.DoesNotExist:
        return Response({'detail': 'Account not found.'}, status=status.HTTP_404_NOT_FOUND)

    try:
        tx = deposit(
            account_id=str(account.id),
            amount=data['amount'],
            description=data.get('description', 'Deposit'),
            initiated_by=request.user,
            idempotency_key=data.get('idempotency_key') or None,
        )
        send_transaction_notification.delay(str(tx.id))
        return Response(TransactionSerializer(tx).data, status=status.HTTP_201_CREATED)
    except (AccountStatusError, TransactionError) as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def withdraw_view(request):
    serializer = WithdrawSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    try:
        account = Account.objects.get(id=data['account_id'], owner=request.user)
    except Account.DoesNotExist:
        return Response({'detail': 'Account not found.'}, status=status.HTTP_404_NOT_FOUND)

    try:
        tx = withdraw(
            account_id=str(account.id),
            amount=data['amount'],
            description=data.get('description', 'Withdrawal'),
            initiated_by=request.user,
            idempotency_key=data.get('idempotency_key') or None,
        )
        send_transaction_notification.delay(str(tx.id))
        return Response(TransactionSerializer(tx).data, status=status.HTTP_201_CREATED)
    except InsufficientFundsError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except (AccountStatusError, TransactionError) as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def transfer_view(request):
    serializer = TransferSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    try:
        from_account = Account.objects.get(id=data['from_account_id'], owner=request.user)
    except Account.DoesNotExist:
        return Response({'detail': 'Source account not found.'}, status=status.HTTP_404_NOT_FOUND)

    tt = data.get('transfer_type', 'TRANSFER_INTERNAL')
    dest_number = data.get('to_account_number')

    try:
        if tt in (
            Transaction.TransactionType.TRANSFER_INTERNAL,
            Transaction.TransactionType.TRANSFER_EXTERNAL,
        ):
            preview = preview_transfer_fees_for_account_number(
                str(from_account.id),
                dest_number,
                data['amount'],
                tt,
            )
        elif data.get('to_account_resolved_id'):
            preview = preview_transfer_fees(
                str(from_account.id),
                data['to_account_resolved_id'],
                data['amount'],
                tt,
            )
        else:
            preview = preview_transfer_fees_for_account_number(
                str(from_account.id),
                dest_number,
                data['amount'],
                tt,
            )
    except TransactionError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    requires_reg = preview.get('requires_regulated_session')
    regulated_sid = data.get('regulated_session_id')

    if requires_reg:
        if not regulated_sid:
            return Response(
                {
                    'detail': 'International compliance fees require verification. Start a session, pay each fee, '
                    'confirm each email code, then submit the transfer with regulated_session_id.',
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            regulated_session = assert_session_ready_for_international_transfer(
                regulated_sid,
                request.user,
                str(from_account.id),
                dest_number,
                data['amount'],
                international_wire=data.get('international_wire'),
            )
        except RegulatedFlowError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        if not regulated_session.transfer_transaction_id:
            return Response(
                {'detail': 'No pending transfer is linked to this session. Start verification again.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
    # Transfer email OTP was already verified when starting the regulated session (see regulated_intl_session_start).
    elif tt in (
        Transaction.TransactionType.TRANSFER_INTERNAL,
        Transaction.TransactionType.TRANSFER_EXTERNAL,
    ) or preview.get('requires_otp'):
        otp_in = (data.get('otp') or '').strip()
        if len(otp_in) != 6 or not otp_in.isdigit():
            return Response(
                {'detail': 'This transfer requires a 6-digit email verification code.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        row = (
            EmailOTPToken.objects.filter(user=request.user, purpose='transfer_auth', is_used=False)
            .order_by('-created_at')
            .first()
        )
        if not row or not row.is_valid() or row.token != otp_in:
            return Response(
                {'detail': 'Invalid or expired verification code.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        row.is_used = True
        row.save(update_fields=['is_used'])

    try:
        if requires_reg and regulated_sid:
            tx = complete_pending_international_transfer(
                str(regulated_session.transfer_transaction_id),
                request.user,
            )
            mark_international_session_completed(regulated_sid)
        else:
            recipient_meta = build_transfer_recipient_metadata(
                transfer_type=tt,
                to_account_number=dest_number,
                account_holder_name=data.get('account_holder_name'),
                external_bank_name=data.get('external_bank_name'),
            )
            tx = record_outbound_transfer(
                from_account_id=str(from_account.id),
                destination_account_number=dest_number,
                amount=data['amount'],
                description=data.get('description', 'Transfer'),
                initiated_by=request.user,
                tx_type=tt,
                idempotency_key=data.get('idempotency_key') or None,
                recipient_metadata=recipient_meta or None,
                international_wire_details=data.get('international_wire'),
            )
        send_transaction_notification.delay(str(tx.id))
        return Response(TransactionSerializer(tx).data, status=status.HTTP_201_CREATED)
    except InsufficientFundsError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except (AccountStatusError, TransactionError) as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def transfer_preview_view(request):
    serializer = TransferPreviewSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    try:
        from_account = Account.objects.get(id=data['from_account_id'], owner=request.user)
    except Account.DoesNotExist:
        return Response({'detail': 'Source account not found.'}, status=status.HTTP_404_NOT_FOUND)

    try:
        if data.get('to_account_number'):
            preview = preview_transfer_fees_for_account_number(
                str(from_account.id),
                data['to_account_number'],
                data['amount'],
                data.get('transfer_type', 'TRANSFER_INTERNAL'),
            )
        else:
            preview = preview_transfer_fees(
                str(from_account.id),
                data['to_account_resolved_id'],
                data['amount'],
                data.get('transfer_type', 'TRANSFER_INTERNAL'),
            )
        return Response(preview)
    except TransactionError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def transfer_send_otp_view(request):
    serializer = TransferSendOtpSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    try:
        from_account = Account.objects.get(id=data['from_account_id'], owner=request.user)
    except Account.DoesNotExist:
        return Response({'detail': 'Source account not found.'}, status=status.HTTP_404_NOT_FOUND)

    try:
        if data.get('to_account_number'):
            preview = preview_transfer_fees_for_account_number(
                str(from_account.id),
                data['to_account_number'],
                data['amount'],
                data.get('transfer_type', 'TRANSFER_INTERNAL'),
            )
        else:
            preview = preview_transfer_fees(
                str(from_account.id),
                data['to_account_resolved_id'],
                data['amount'],
                data.get('transfer_type', 'TRANSFER_INTERNAL'),
            )
    except TransactionError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    tt = data.get('transfer_type', Transaction.TransactionType.TRANSFER_INTERNAL)
    domestic_otp = tt in (
        Transaction.TransactionType.TRANSFER_INTERNAL,
        Transaction.TransactionType.TRANSFER_EXTERNAL,
    )
    if not domestic_otp and not preview.get('requires_otp'):
        return Response(
            {'detail': 'No email verification is required for this transfer.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    invalidate_unused_email_otps(request.user, 'transfer_auth')
    code = create_email_otp(request.user, 'transfer_auth')
    queue_email_notification(
        str(request.user.id),
        'mfa_otp',
        {'otp': code, 'full_name': request.user.full_name},
    )
    return Response({'detail': 'Verification code sent to your email.'}, status=status.HTTP_200_OK)


class ExchangeRateListView(generics.ListAPIView):
    queryset = ExchangeRate.objects.all()
    serializer_class = ExchangeRateSerializer
    permission_classes = [CustomerAccessPermission]


class TransactionFeeListView(generics.ListAPIView):
    queryset = TransactionFee.objects.filter(is_active=True)
    serializer_class = TransactionFeeSerializer
    permission_classes = [CustomerAccessPermission]
