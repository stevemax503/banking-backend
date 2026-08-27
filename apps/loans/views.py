from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from apps.users.permissions import CustomerAccessPermission
from rest_framework.response import Response

from .models import LoanProduct, LoanApplication, LoanAccount
from .serializers import (
    LoanProductSerializer, LoanApplicationSerializer, LoanApplicationCreateSerializer,
    LoanAccountSerializer, LoanPaymentSerializer,
)
from .services import make_loan_payment


class LoanProductListView(generics.ListAPIView):
    serializer_class = LoanProductSerializer
    permission_classes = [CustomerAccessPermission]

    def get_queryset(self):
        return LoanProduct.objects.filter(is_active=True).order_by('loan_type', 'name')


class LoanApplicationListCreateView(generics.ListCreateAPIView):
    permission_classes = [CustomerAccessPermission]

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return LoanApplicationCreateSerializer
        return LoanApplicationSerializer

    def get_queryset(self):
        return LoanApplication.objects.filter(applicant=self.request.user).select_related('product')

    def perform_create(self, serializer):
        serializer.save(applicant=self.request.user, status=LoanApplication.Status.SUBMITTED)


class LoanApplicationDetailView(generics.RetrieveAPIView):
    serializer_class = LoanApplicationSerializer
    permission_classes = [CustomerAccessPermission]

    def get_queryset(self):
        return LoanApplication.objects.filter(applicant=self.request.user).select_related('product')

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        data = serializer.data
        from apps.transactions.regulated_flow import loan_payout_context

        data['payout_context'] = loan_payout_context(instance, request.user)
        return Response(data)


class LoanAccountListView(generics.ListAPIView):
    serializer_class = LoanAccountSerializer
    permission_classes = [CustomerAccessPermission]

    def get_queryset(self):
        return LoanAccount.objects.filter(
            application__applicant=self.request.user
        ).prefetch_related('schedule')


class LoanAccountDetailView(generics.RetrieveAPIView):
    serializer_class = LoanAccountSerializer
    permission_classes = [CustomerAccessPermission]

    def get_queryset(self):
        return LoanAccount.objects.filter(
            application__applicant=self.request.user
        ).prefetch_related('schedule')


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def loan_payment(request):
    serializer = LoanPaymentSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    try:
        loan_account = LoanAccount.objects.get(
            id=data['loan_account_id'],
            application__applicant=request.user,
        )
    except LoanAccount.DoesNotExist:
        return Response({'detail': 'Loan account not found.'}, status=status.HTTP_404_NOT_FOUND)

    try:
        from apps.accounts.models import Account
        from_account = Account.objects.get(id=data['account_id'], owner=request.user)
        tx = make_loan_payment(
            loan_account_id=str(loan_account.id),
            from_account_id=str(from_account.id),
            amount=data['amount'],
            initiated_by=request.user,
        )
        return Response({'detail': 'Payment successful.', 'reference': tx.reference_number})
    except Exception as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
