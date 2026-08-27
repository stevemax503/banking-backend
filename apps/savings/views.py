from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ValidationError
from apps.users.permissions import CustomerAccessPermission
from rest_framework.response import Response

from apps.transactions.services import InsufficientFundsError

from .models import SavingsGoal
from .serializers import (
    SavingsGoalAllocateSerializer,
    SavingsGoalCreateSerializer,
    SavingsGoalSerializer,
    SavingsGoalUpdateSerializer,
)
from apps.notifications.services import send_transaction_notification

from .services import allocate_to_goal, cancel_savings_goal


class SavingsGoalListCreateView(generics.ListCreateAPIView):
    permission_classes = [CustomerAccessPermission]
    pagination_class = None

    def get_queryset(self):
        return SavingsGoal.objects.filter(
            owner=self.request.user,
            status=SavingsGoal.Status.ACTIVE,
        )

    def get_serializer_class(self):
        if self.request.method == 'POST':
            return SavingsGoalCreateSerializer
        return SavingsGoalSerializer

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user, status=SavingsGoal.Status.ACTIVE)


class SavingsGoalDetailView(generics.RetrieveUpdateAPIView):
    permission_classes = [CustomerAccessPermission]
    lookup_field = 'pk'

    def get_queryset(self):
        return SavingsGoal.objects.filter(owner=self.request.user)

    def get_serializer_class(self):
        if self.request.method in ('PUT', 'PATCH'):
            return SavingsGoalUpdateSerializer
        return SavingsGoalSerializer

    def perform_update(self, serializer):
        if serializer.instance.status != SavingsGoal.Status.ACTIVE:
            raise ValidationError({'detail': 'Cannot edit a cancelled goal.'})
        serializer.save()


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def savings_goal_cancel(request, pk):
    try:
        goal = cancel_savings_goal(pk, request.user)
    except SavingsGoal.DoesNotExist:
        return Response({'detail': 'Goal not found.'}, status=status.HTTP_404_NOT_FOUND)
    except ValueError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(SavingsGoalSerializer(goal).data)


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def savings_goal_allocate(request, pk):
    ser = SavingsGoalAllocateSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    amount = ser.validated_data['amount']
    try:
        goal, tx = allocate_to_goal(pk, request.user, amount)
    except SavingsGoal.DoesNotExist:
        return Response({'detail': 'Goal not found.'}, status=status.HTTP_404_NOT_FOUND)
    except ValueError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except InsufficientFundsError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    send_transaction_notification.delay(str(tx.id))
    return Response(SavingsGoalSerializer(goal).data, status=status.HTTP_200_OK)
