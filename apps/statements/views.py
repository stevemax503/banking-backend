import mimetypes
from django.http import FileResponse, Http404
from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from apps.users.permissions import CustomerAccessPermission
from rest_framework.response import Response

from .models import Statement
from .serializers import StatementSerializer, StatementRequestSerializer
from .tasks import generate_statement_task


class StatementListView(generics.ListAPIView):
    serializer_class = StatementSerializer
    permission_classes = [CustomerAccessPermission]

    def get_queryset(self):
        return Statement.objects.filter(
            account__owner=self.request.user
        ).select_related('account')

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['request'] = self.request
        return context


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def request_statement(request):
    serializer = StatementRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    from apps.accounts.models import Account
    try:
        account = Account.objects.get(id=data['account_id'], owner=request.user)
    except Account.DoesNotExist:
        return Response({'detail': 'Account not found.'}, status=status.HTTP_404_NOT_FOUND)

    generate_statement_task.delay(
        str(account.id),
        data['period_start'].isoformat(),
        data['period_end'].isoformat(),
        to_email=data['email'],
        e_signed=data.get('e_signed', False),
    )
    return Response(
        {
            'detail': f'Statement is being generated and will be emailed to {data["email"]} when ready.',
            'email': data['email'],
        }
    )


@api_view(['GET'])
@permission_classes([CustomerAccessPermission])
def download_statement(request, pk):
    try:
        statement = Statement.objects.get(id=pk, account__owner=request.user)
    except Statement.DoesNotExist:
        raise Http404
    if not statement.pdf_file:
        return Response({'detail': 'PDF not yet generated.'}, status=status.HTTP_404_NOT_FOUND)
    return FileResponse(statement.pdf_file.open('rb'), content_type='application/pdf')
