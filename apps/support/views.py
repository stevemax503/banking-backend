from django.utils import timezone
from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from apps.users.permissions import CustomerAccessPermission
from rest_framework.response import Response

from .models import SupportTicket, TicketMessage
from .serializers import SupportTicketSerializer, SupportTicketCreateSerializer, AddMessageSerializer


class TicketListCreateView(generics.ListCreateAPIView):
    permission_classes = [CustomerAccessPermission]

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return SupportTicketCreateSerializer
        return SupportTicketSerializer

    def get_queryset(self):
        return SupportTicket.objects.filter(
            customer=self.request.user
        ).prefetch_related('messages__author')

    def perform_create(self, serializer):
        ticket = serializer.save(customer=self.request.user)
        return ticket


class TicketDetailView(generics.RetrieveAPIView):
    serializer_class = SupportTicketSerializer
    permission_classes = [CustomerAccessPermission]

    def get_queryset(self):
        return SupportTicket.objects.filter(customer=self.request.user)


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def add_message(request, pk):
    try:
        ticket = SupportTicket.objects.get(id=pk, customer=request.user)
    except SupportTicket.DoesNotExist:
        return Response({'detail': 'Ticket not found.'}, status=status.HTTP_404_NOT_FOUND)

    if ticket.status == SupportTicket.Status.CLOSED:
        return Response({'detail': 'Cannot message on a closed ticket.'}, status=status.HTTP_400_BAD_REQUEST)

    serializer = AddMessageSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    serializer.save(ticket=ticket, author=request.user)
    return Response({'detail': 'Message added.'}, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def close_ticket(request, pk):
    try:
        ticket = SupportTicket.objects.get(id=pk, customer=request.user)
    except SupportTicket.DoesNotExist:
        return Response({'detail': 'Ticket not found.'}, status=status.HTTP_404_NOT_FOUND)

    ticket.status = SupportTicket.Status.CLOSED
    ticket.resolved_at = timezone.now()
    ticket.save()
    return Response({'detail': 'Ticket closed.'})
