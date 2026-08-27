from decimal import Decimal

from django.db.models import Prefetch, Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from apps.users.permissions import CustomerAccessPermission
from rest_framework.response import Response

from apps.accounts.models import Account
from apps.accounts.serializers import AccountSerializer
from apps.transactions.models import Transaction

from .models import CardIssuance, CardProductConfig
from .serializers import CardIssuanceSerializer, CardProductConfigSerializer
from .services import (
    CardServiceError,
    InsufficientFundsError,
    pay_card_issuance_fee,
    request_card_for_account,
    request_card_replacement,
)


def _month_start():
    n = timezone.now()
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _issuance_for_account_summary(account: Account) -> CardIssuance | None:
    """Prefer pending payment (replacement flow), else latest active issuance."""
    rows = list(account.card_issuances.all())
    for iss in rows:
        if iss.status == CardIssuance.Status.PENDING_PAYMENT:
            return iss
    for iss in rows:
        if iss.status == CardIssuance.Status.ACTIVE:
            return iss
    return None


def _month_debit_total(account_id) -> Decimal:
    total = Transaction.objects.filter(
        from_account_id=account_id,
        created_at__gte=_month_start(),
        status=Transaction.Status.COMPLETED,
    ).aggregate(s=Sum('amount'))['s']
    return Decimal(str(total or 0))


@api_view(['GET'])
@permission_classes([CustomerAccessPermission])
def card_summary(request):
    """Accounts with optional card issuance + month-to-date debit total (for limit bar)."""
    accounts = (
        Account.objects.filter(owner=request.user, exclude_from_card_summary=False)
        .select_related('currency')
        .prefetch_related(
            Prefetch(
                'card_issuances',
                queryset=CardIssuance.objects.order_by('-requested_at'),
            ),
        )
        .order_by('-is_primary', '-created_at')
    )
    configs = {c.account_type: c for c in CardProductConfig.objects.filter(is_active=True)}
    out = []
    for acc in accounts:
        iss = _issuance_for_account_summary(acc)
        cfg = configs.get(acc.account_type)
        out.append(
            {
                'account': AccountSerializer(acc, context={'request': request}).data,
                'issuance': CardIssuanceSerializer(iss, context={'request': request}).data if iss else None,
                'current_month_spend': _month_debit_total(acc.id),
                'product': CardProductConfigSerializer(cfg, context={'request': request}).data if cfg else None,
            },
        )
    return Response(out)


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def request_card(request):
    account_id = request.data.get('account_id')
    if not account_id:
        return Response({'detail': 'account_id is required.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        account = Account.objects.get(id=account_id, owner=request.user)
    except Account.DoesNotExist:
        return Response({'detail': 'Account not found.'}, status=status.HTTP_404_NOT_FOUND)
    try:
        issuance = request_card_for_account(request.user, account)
    except CardServiceError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(CardIssuanceSerializer(issuance, context={'request': request}).data, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def request_card_replacement_view(request):
    account_id = request.data.get('account_id')
    if not account_id:
        return Response({'detail': 'account_id is required.'}, status=status.HTTP_400_BAD_REQUEST)
    raw = request.data.get('terminate_previous', True)
    if isinstance(raw, str):
        terminate_previous = raw.strip().lower() in ('1', 'true', 'yes', 'on')
    else:
        terminate_previous = bool(raw)
    try:
        account = Account.objects.get(id=account_id, owner=request.user)
    except Account.DoesNotExist:
        return Response({'detail': 'Account not found.'}, status=status.HTTP_404_NOT_FOUND)
    try:
        issuance = request_card_replacement(request.user, account, terminate_previous=terminate_previous)
    except CardServiceError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(CardIssuanceSerializer(issuance, context={'request': request}).data, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def pay_card_fee(request, issuance_id):
    try:
        issuance = pay_card_issuance_fee(request.user, issuance_id)
    except CardServiceError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except InsufficientFundsError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(CardIssuanceSerializer(issuance, context={'request': request}).data)
