"""APIs for per-line compliance fees + OTP (international transfer & loan payout)."""
from decimal import Decimal

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from apps.users.permissions import CustomerAccessPermission
from rest_framework.response import Response

from apps.accounts.models import Account
from apps.users.models import EmailOTPToken
from apps.loans.models import LoanApplication
from apps.loans.services import disburse_loan

from .serializers import (
    RegulatedIntlSessionStartSerializer,
    RegulatedLineOtpSerializer,
    LoanRegulatedPayoutStartSerializer,
    LoanRegulatedPayoutCompleteSerializer,
)
from .services import (
    InsufficientFundsError,
    TransactionError,
    preview_transfer_fees_for_account_number,
)
from .regulated_flow import (
    RegulatedFlowError,
    regulated_line_generate_feedback_message,
    session_serialized,
    start_international_session,
    start_loan_payout_session,
    verify_line_otp,
    assert_session_ready_for_loan_disburse,
    mark_loan_session_completed,
    mark_international_session_completed,
)
from .models import Transaction
from .services import complete_pending_international_transfer, TransactionError
from .serializers import TransactionSerializer
from apps.notifications.services import send_transaction_notification


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def regulated_intl_session_start(request):
    ser = RegulatedIntlSessionStartSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    data = ser.validated_data
    try:
        from_acc = Account.objects.get(id=data['from_account_id'], owner=request.user)
    except Account.DoesNotExist:
        return Response({'detail': 'Source account not found.'}, status=status.HTTP_404_NOT_FOUND)
    dest_number = data['to_account_number']
    try:
        preview = preview_transfer_fees_for_account_number(
            str(from_acc.id),
            dest_number,
            data['amount'],
            data.get('transfer_type', 'TRANSFER_INTERNATIONAL'),
        )
    except TransactionError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    if not preview.get('requires_regulated_session'):
        return Response(
            {'detail': 'This transfer does not require a compliance verification session.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if preview.get('requires_otp'):
        otp_in = (data.get('transfer_otp') or '').strip()
        if len(otp_in) != 6 or not otp_in.isdigit():
            return Response(
                {'detail': 'Enter the 6-digit transfer verification code from your email.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        row = (
            EmailOTPToken.objects.filter(user=request.user, purpose='transfer_auth', is_used=False)
            .order_by('-created_at')
            .first()
        )
        if not row or not row.is_valid() or row.token != otp_in:
            return Response(
                {'detail': 'Invalid or expired transfer verification code.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        row.is_used = True
        row.save(update_fields=['is_used'])
    try:
        from .services import build_transfer_recipient_metadata

        session = start_international_session(
            request.user,
            from_acc,
            dest_number,
            data['amount'],
            data.get('transfer_type', 'TRANSFER_INTERNATIONAL'),
            data.get('description', ''),
            idempotency_key=data.get('idempotency_key') or None,
            international_wire_details=data.get('international_wire'),
            recipient_metadata=build_transfer_recipient_metadata(
                transfer_type='TRANSFER_INTERNATIONAL',
                to_account_number=dest_number,
            ),
        )
    except RegulatedFlowError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    from .regulated_flow import sync_session_compliance_lines

    sync_session_compliance_lines(session)
    session.refresh_from_db()
    return Response(session_serialized(session), status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def regulated_session_complete_transfer(request, session_id):
    """Mark a pending international transfer COMPLETED after all compliance lines are verified."""
    from .regulated_models import RegulatedTransferSession

    try:
        session = RegulatedTransferSession.objects.select_related('transfer_transaction').get(
            id=session_id,
            user=request.user,
        )
    except RegulatedTransferSession.DoesNotExist:
        return Response({'detail': 'Session not found.'}, status=status.HTTP_404_NOT_FOUND)

    if session.flow != RegulatedTransferSession.Flow.INTERNATIONAL_TRANSFER:
        return Response({'detail': 'Invalid session type.'}, status=status.HTTP_400_BAD_REQUEST)
    if timezone.now() > session.expires_at:
        return Response({'detail': 'Session expired.'}, status=status.HTTP_400_BAD_REQUEST)
    if session.status != RegulatedTransferSession.Status.LINES_VERIFIED:
        return Response(
            {'detail': 'Complete all compliance fee verification steps first.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not session.transfer_transaction_id:
        return Response(
            {'detail': 'No pending transfer is linked to this session.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    tx = session.transfer_transaction
    if tx.status == Transaction.Status.COMPLETED:
        mark_international_session_completed(session_id)
        return Response(TransactionSerializer(tx).data, status=status.HTTP_200_OK)

    try:
        tx = complete_pending_international_transfer(str(tx.id), request.user)
        mark_international_session_completed(session_id)
    except TransactionError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    send_transaction_notification.delay(str(tx.id))
    return Response(TransactionSerializer(tx).data, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([CustomerAccessPermission])
def regulated_session_detail(request, session_id):
    from .regulated_models import RegulatedTransferSession

    try:
        session = RegulatedTransferSession.objects.prefetch_related('lines__fee_line').get(
            id=session_id,
            user=request.user,
        )
    except RegulatedTransferSession.DoesNotExist:
        return Response({'detail': 'Session not found.'}, status=status.HTTP_404_NOT_FOUND)

    if session.status == RegulatedTransferSession.Status.CANCELLED:
        return Response(
            {'detail': 'This compliance session was cancelled. Start a new one.'},
            status=status.HTTP_410_GONE,
        )

    from .regulated_flow import sync_session_compliance_lines

    sync_session_compliance_lines(session)
    session.refresh_from_db()
    return Response(session_serialized(session))


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def regulated_line_submit_payment(request, session_id, line_id):
    from .regulated_models import RegulatedTransferSessionLine
    from .regulated_flow import RegulatedFlowError, session_serialized, submit_external_payment

    try:
        line = RegulatedTransferSessionLine.objects.select_related('session', 'fee_line').get(
            id=line_id,
            session_id=session_id,
            session__user=request.user,
        )
    except RegulatedTransferSessionLine.DoesNotExist:
        return Response({'detail': 'Fee line not found.'}, status=status.HTTP_404_NOT_FOUND)

    try:
        payment_proof = request.FILES.get('payment_proof')
        submit_external_payment(line.id, request.user, payment_proof=payment_proof)
    except RegulatedFlowError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    line.session.refresh_from_db()
    return Response(
        {
            'detail': 'Payment submitted. We will email your verification code once confirmed.',
            'session': session_serialized(line.session),
        },
        status=status.HTTP_200_OK,
    )


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def regulated_line_charge_send_otp(request, session_id, line_id):
    """Resend OTP when line is already CHARGED."""
    from .regulated_models import RegulatedTransferSessionLine
    from .regulated_flow import RegulatedFlowError, send_compliance_line_otp

    try:
        line = RegulatedTransferSessionLine.objects.select_related('session', 'fee_line').get(
            id=line_id,
            session_id=session_id,
            session__user=request.user,
        )
    except RegulatedTransferSessionLine.DoesNotExist:
        return Response({'detail': 'Fee line not found.'}, status=status.HTTP_404_NOT_FOUND)

    if line.status != RegulatedTransferSessionLine.Status.CHARGED:
        return Response(
            {'detail': 'Your verification code will be sent after payment is confirmed.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        send_compliance_line_otp(line.id, request.user, staff_issued=False)
    except RegulatedFlowError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    return Response({'detail': 'Verification code resent to your email.'}, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def regulated_line_verify_otp(request, session_id, line_id):
    ser = RegulatedLineOtpSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    from .regulated_models import RegulatedTransferSessionLine

    try:
        line = RegulatedTransferSessionLine.objects.select_related('session').get(
            id=line_id,
            session_id=session_id,
            session__user=request.user,
        )
    except RegulatedTransferSessionLine.DoesNotExist:
        return Response({'detail': 'Fee line not found.'}, status=status.HTTP_404_NOT_FOUND)
    try:
        verify_line_otp(line.id, request.user, ser.validated_data['otp'])
    except RegulatedFlowError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    session = line.session
    return Response(
        {
            'detail': 'Step verified.',
            'session': session_serialized(session),
        },
        status=status.HTTP_200_OK,
    )


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def loan_regulated_payout_start(request, application_id):
    ser = LoanRegulatedPayoutStartSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    try:
        app = LoanApplication.objects.select_related('product').get(id=application_id, applicant=request.user)
    except LoanApplication.DoesNotExist:
        return Response({'detail': 'Application not found.'}, status=status.HTTP_404_NOT_FOUND)
    try:
        from_acc = Account.objects.get(
            id=ser.validated_data['disbursement_account_id'], owner=request.user,
        )
    except Account.DoesNotExist:
        return Response({'detail': 'Disbursement account not found.'}, status=status.HTTP_404_NOT_FOUND)
    from .regulated_flow import loan_payout_context

    ctx = loan_payout_context(app, request.user)
    if not ctx['requires_compliance']:
        return Response(
            {
                'detail': 'No compliance fees apply. Use direct disbursement instead.',
                'requires_compliance': False,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        session = start_loan_payout_session(
            request.user,
            from_acc,
            app,
            idempotency_key=ser.validated_data.get('idempotency_key') or None,
        )
    except RegulatedFlowError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    payload = session_serialized(session)
    payload['requires_compliance'] = True
    return Response(payload, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def loan_regulated_payout_complete(request, application_id):
    ser = LoanRegulatedPayoutCompleteSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    try:
        app = LoanApplication.objects.get(id=application_id, applicant=request.user)
    except LoanApplication.DoesNotExist:
        return Response({'detail': 'Application not found.'}, status=status.HTTP_404_NOT_FOUND)
    try:
        disb_acc = Account.objects.get(id=ser.validated_data['disbursement_account_id'], owner=request.user)
    except Account.DoesNotExist:
        return Response({'detail': 'Disbursement account not found.'}, status=status.HTTP_404_NOT_FOUND)
    from .regulated_flow import loan_payout_context

    ctx = loan_payout_context(app, request.user)
    sid = ser.validated_data.get('regulated_session_id')
    if ctx['requires_compliance']:
        if not sid:
            from .regulated_flow import get_active_loan_payout_session
            from apps.transactions.regulated_models import RegulatedTransferSession

            active = get_active_loan_payout_session(app, request.user)
            if (
                active
                and active.status == RegulatedTransferSession.Status.LINES_VERIFIED
            ):
                sid = active.id
            else:
                return Response(
                    {'detail': 'regulated_session_id is required when compliance fees apply.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        try:
            assert_session_ready_for_loan_disburse(sid, request.user, application_id)
        except RegulatedFlowError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    elif sid:
        return Response(
            {'detail': 'No compliance session is required for this loan.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        disburse_loan(str(app.id), str(disb_acc.id), request.user, enforce_applicant_account=True)
    except ValueError as e:
        return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    if sid:
        mark_loan_session_completed(sid)
    return Response({'detail': 'Loan funds sent to your account.'}, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([CustomerAccessPermission])
def loan_payout_context_view(request, application_id):
    try:
        app = LoanApplication.objects.select_related('product').get(
            id=application_id, applicant=request.user,
        )
    except LoanApplication.DoesNotExist:
        return Response({'detail': 'Application not found.'}, status=status.HTTP_404_NOT_FOUND)
    from .regulated_flow import loan_payout_context

    return Response(loan_payout_context(app, request.user))
