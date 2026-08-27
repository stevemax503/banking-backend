from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from apps.users.permissions import CustomerAccessPermission
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter

from .models import Account, Currency
from .serializers import (
    AccountSerializer,
    AccountCreateSerializer,
    AccountPartialUpdateSerializer,
    CurrencySerializer,
    AccountStatusSerializer,
)
from .services import create_additional_uae_account
from apps.audit.mixins import AuditMixin
from apps.audit.models import AuditLog, log_action
from apps.audit.middleware import AuditMiddleware
from apps.audit.customer_audit import mark_audit_handled


class AccountListCreateView(AuditMixin, generics.ListCreateAPIView):
    permission_classes = [CustomerAccessPermission]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['account_type', 'status', 'currency']
    search_fields = ['account_number', 'nickname']
    ordering_fields = ['created_at', 'balance', 'is_primary']
    ordering = ['-is_primary', '-created_at']

    def get_queryset(self):
        return Account.objects.filter(owner=self.request.user, exclude_from_card_summary=False).select_related(
            'currency',
        )

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return AccountCreateSerializer
        return AccountSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        v = serializer.validated_data
        account = create_additional_uae_account(
            request.user,
            v['account_type'],
            v['currency'],
            v.get('nickname') or '',
        )
        read = AccountSerializer(account, context={'request': request})
        log_action(
            actor=request.user,
            action=AuditLog.Action.CREATE,
            target_model='Account',
            target_id=account.pk,
            new_value=dict(read.data),
            description=f"Opened account {account.account_type} ({account.currency_id})",
            ip_address=AuditMiddleware.get_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', ''),
        )
        mark_audit_handled(request)
        return Response(read.data, status=status.HTTP_201_CREATED)


class AccountDetailView(AuditMixin, generics.RetrieveUpdateAPIView):
    permission_classes = [CustomerAccessPermission]

    def get_queryset(self):
        return Account.objects.filter(owner=self.request.user, exclude_from_card_summary=False).select_related(
            'currency',
        )

    def get_serializer_class(self):
        if self.request.method in ['PUT', 'PATCH']:
            return AccountPartialUpdateSerializer
        return AccountSerializer

    def perform_update(self, serializer):
        old_snapshot = AccountSerializer(self.get_object(), context={'request': self.request}).data
        instance = serializer.save()
        if serializer.validated_data.get('is_primary') is True:
            Account.objects.filter(owner=instance.owner).exclude(pk=instance.pk).update(is_primary=False)
            if not instance.is_primary:
                instance.is_primary = True
                instance.save(update_fields=['is_primary'])
        new_snapshot = AccountSerializer(instance, context={'request': self.request}).data
        log_action(
            actor=self.request.user,
            action=AuditLog.Action.UPDATE,
            target_model='Account',
            target_id=instance.pk,
            old_value=dict(old_snapshot),
            new_value=dict(new_snapshot),
            description='Updated account settings',
            ip_address=AuditMiddleware.get_client_ip(self.request),
            user_agent=self.request.META.get('HTTP_USER_AGENT', ''),
        )
        mark_audit_handled(self.request)


class CurrencyListView(generics.ListAPIView):
    queryset = Currency.objects.filter(is_active=True)
    serializer_class = CurrencySerializer
    permission_classes = [CustomerAccessPermission]
