from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from apps.users.permissions import CustomerAccessPermission
from rest_framework.response import Response

from apps.accounts.models import Account
from apps.admin_portal.permissions import IsAdminUser
from apps.notifications.services import send_transaction_notification
from apps.transactions.serializers import TransactionSerializer
from apps.transactions.services import (
    AccountStatusError,
    InsufficientFundsError,
    TransactionError,
    withdraw,
)

from .models import PaymentFeeSettings, PaymentManagementFeeOverride
from .serializers import (
    BillPaySerializer,
    PaymentFeeSettingsSerializer,
    PaymentManagementFeeOverrideSerializer,
)
from .services import resolve_management_fee


@api_view(['GET'])
@permission_classes([CustomerAccessPermission])
def resolve_management_fee_view(request):
    service_id = (request.query_params.get('service_id') or '').strip()
    biller_id = (request.query_params.get('biller_id') or '').strip()
    if not service_id or not biller_id:
        return Response({'detail': 'service_id and biller_id are required.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        fee = resolve_management_fee(service_id, biller_id)
    except ValueError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    default_fee = PaymentFeeSettings.get_solo().default_management_fee
    is_override = PaymentManagementFeeOverride.objects.filter(
        service_id=service_id,
        biller_id=biller_id,
    ).exists()
    return Response(
        {
            'management_fee': str(fee),
            'default_management_fee': str(default_fee),
            'is_override': is_override,
        }
    )


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def bill_pay_view(request):
    serializer = BillPaySerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    try:
        account = Account.objects.get(id=data['account_id'], owner=request.user)
    except Account.DoesNotExist:
        return Response({'detail': 'Account not found.'}, status=status.HTTP_404_NOT_FOUND)

    try:
        mgmt = resolve_management_fee(data['service_id'], data['biller_id'])
    except ValueError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    try:
        tx = withdraw(
            account_id=str(account.id),
            amount=data['amount'],
            description=data['description'],
            initiated_by=request.user,
            idempotency_key=data.get('idempotency_key') or None,
            additional_fee=mgmt,
        )
        send_transaction_notification.delay(str(tx.id))
    except InsufficientFundsError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except (AccountStatusError, TransactionError) as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(
        {
            'transaction': TransactionSerializer(tx).data,
            'management_fee': str(mgmt),
        },
        status=status.HTTP_201_CREATED,
    )


class AdminPaymentFeeSettingsView(generics.RetrieveUpdateAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = PaymentFeeSettingsSerializer

    def get_object(self):
        return PaymentFeeSettings.get_solo()


class AdminPaymentFeeOverrideListCreateView(generics.ListAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = PaymentManagementFeeOverrideSerializer
    queryset = PaymentManagementFeeOverride.objects.all().order_by('service_id', 'biller_id')

    def post(self, request, *args, **kwargs):
        serializer = PaymentManagementFeeOverrideSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        obj, created = PaymentManagementFeeOverride.objects.update_or_create(
            service_id=d['service_id'],
            biller_id=d['biller_id'],
            defaults={
                'management_fee': d['management_fee'],
                'biller_label': d.get('biller_label') or '',
            },
        )
        return Response(
            PaymentManagementFeeOverrideSerializer(obj).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class AdminPaymentFeeOverrideDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = PaymentManagementFeeOverrideSerializer
    queryset = PaymentManagementFeeOverride.objects.all()
