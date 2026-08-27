import logging
import secrets
import pyotp
from datetime import timedelta
from django.utils import timezone
from django.contrib.auth import get_user_model
from rest_framework import status, generics
from rest_framework.exceptions import PermissionDenied
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from .permissions import CustomerAccessPermission
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import RefreshToken
from drf_spectacular.utils import extend_schema

from .models import PasswordResetToken, EmailOTPToken, ProfileChangeRequest
from .email_otp import create_email_otp
from .serializers import (
    UserRegistrationSerializer, UserProfileSerializer, UserProfileUpdateSerializer,
    ChangePasswordSerializer, PasswordResetRequestSerializer, PasswordResetConfirmSerializer,
    MFAVerifySerializer, KYCUploadSerializer, ProfileChangeRequestCreateSerializer,
)
from apps.notifications.services import queue_email_notification, send_email_notification
from apps.accounts.services import provision_primary_bank_account
from apps.audit.models import AuditLog, log_action
from apps.audit.middleware import AuditMiddleware
from apps.audit.customer_audit import (
    log_customer_activity,
    log_customer_by_email,
    mark_audit_handled,
)

User = get_user_model()
logger = logging.getLogger(__name__)


class RegisterView(generics.CreateAPIView):
    serializer_class = UserRegistrationSerializer
    permission_classes = [AllowAny]
    throttle_scope = 'auth'

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        provision_primary_bank_account(user)
        log_action(
            actor=user,
            action=AuditLog.Action.CREATE,
            target_model='CustomUser',
            target_id=user.pk,
            description='Registered new customer account',
            new_value={'email': user.email},
            ip_address=AuditMiddleware.get_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:512],
        )
        try:
            queue_email_notification(
                user_id=str(user.id),
                event_type='registration',
                context={
                    'full_name': user.full_name,
                    'user_email': user.email,
                },
            )
        except Exception:
            logger.exception('Failed to queue welcome email for user %s', user.id)
        return Response(
            {'detail': 'Registration successful. Please verify your email.'},
            status=status.HTTP_201_CREATED,
        )


class LoginView(TokenObtainPairView):
    throttle_scope = 'auth'

    def post(self, request, *args, **kwargs):
        email = (request.data.get('email') or request.data.get('username') or '').strip()
        ip = AuditMiddleware.get_client_ip(request)
        ua = (request.META.get('HTTP_USER_AGENT') or '')[:512]
        try:
            response = super().post(request, *args, **kwargs)
        except Exception:
            if email:
                log_customer_by_email(
                    request,
                    email,
                    action=AuditLog.Action.FAILED_LOGIN,
                    target_model='Session',
                    description='Failed login attempt',
                )
            raise

        if response.status_code == 200:
            try:
                user = User.objects.get(email__iexact=email)
            except User.DoesNotExist:
                return response
            if user.is_mfa_enabled:
                token = create_email_otp(user, 'login_mfa')
                send_email_notification.delay(
                    user_id=str(user.id),
                    event_type='mfa_otp',
                    context={'otp': token, 'full_name': user.full_name},
                )
                if user.is_customer:
                    log_action(
                        actor=user,
                        action=AuditLog.Action.LOGIN,
                        target_model='Session',
                        target_id=str(user.pk),
                        description='Login started — MFA verification sent',
                        ip_address=ip,
                        user_agent=ua,
                    )
                return Response(
                    {'mfa_required': True, 'detail': 'MFA verification required.'},
                    status=status.HTTP_200_OK,
                )
            if user.is_customer:
                log_action(
                    actor=user,
                    action=AuditLog.Action.LOGIN,
                    target_model='Session',
                    target_id=str(user.pk),
                    description='Customer signed in',
                    ip_address=ip,
                    user_agent=ua,
                )
            user.failed_login_attempts = 0
            user.save(update_fields=['failed_login_attempts'])
        elif response.status_code >= 400 and email:
            data = getattr(response, 'data', None)
            if isinstance(data, dict) and data.get('mfa_required'):
                return response
            log_customer_by_email(
                request,
                email,
                action=AuditLog.Action.FAILED_LOGIN,
                target_model='Session',
                description='Failed login attempt',
            )
        return response


@extend_schema(request=MFAVerifySerializer)
@api_view(['POST'])
@permission_classes([AllowAny])
def mfa_verify(request):
    serializer = MFAVerifySerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    email = request.data.get('email')
    token_value = serializer.validated_data['token']
    mfa_type = serializer.validated_data['mfa_type']

    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        return Response({'detail': 'Invalid credentials.'}, status=status.HTTP_400_BAD_REQUEST)

    if mfa_type == 'email':
        otp = EmailOTPToken.objects.filter(
            user=user, purpose='login_mfa', is_used=False, context_id__isnull=True
        ).order_by('-created_at').first()
        if not otp or not otp.is_valid() or otp.token != token_value:
            return Response({'detail': 'Invalid or expired OTP.'}, status=status.HTTP_400_BAD_REQUEST)
        otp.is_used = True
        otp.save()
    elif mfa_type == 'totp':
        # TOTP verification via django-otp
        from django_otp.plugins.otp_totp.models import TOTPDevice
        devices = TOTPDevice.objects.devices_for_user(user, confirmed=True)
        verified = any(d.verify_token(token_value) for d in devices)
        if not verified:
            return Response({'detail': 'Invalid TOTP token.'}, status=status.HTTP_400_BAD_REQUEST)

    refresh = RefreshToken.for_user(user)
    if user.is_customer:
        log_action(
            actor=user,
            action=AuditLog.Action.LOGIN,
            target_model='Session',
            target_id=str(user.pk),
            description='Customer signed in (MFA verified)',
            ip_address=AuditMiddleware.get_client_ip(request),
            user_agent=(request.META.get('HTTP_USER_AGENT') or '')[:512],
        )
    return Response({
        'access': str(refresh.access_token),
        'refresh': str(refresh),
    })


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def logout_view(request):
    try:
        refresh_token = request.data.get('refresh')
        token = RefreshToken(refresh_token)
        token.blacklist()
        log_customer_activity(
            request,
            action=AuditLog.Action.LOGOUT,
            target_model='Session',
            description='Customer signed out',
        )
        mark_audit_handled(request)
        return Response({'detail': 'Successfully logged out.'}, status=status.HTTP_200_OK)
    except Exception:
        return Response({'detail': 'Invalid token.'}, status=status.HTTP_400_BAD_REQUEST)


class ProfileView(generics.RetrieveUpdateAPIView):
    permission_classes = [CustomerAccessPermission]

    def get_serializer_class(self):
        if self.request.method in ['PUT', 'PATCH']:
            return UserProfileUpdateSerializer
        return UserProfileSerializer

    def get_object(self):
        return self.request.user

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        instance.try_complete_profile_setup()
        instance.refresh_from_db()
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    def perform_update(self, serializer):
        user = self.request.user
        if (
            user.is_customer
            and user.has_required_profile_fields()
            and user.accounts.exists()
        ):
            raise PermissionDenied(
                detail='Profile changes must be submitted for admin approval. '
                'Use POST /api/auth/profile/update-request/.'
            )
        instance = serializer.save()
        instance.refresh_from_db()
        instance.try_complete_profile_setup()


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def profile_update_request_create(request):
    user = request.user
    if not user.is_customer:
        return Response({'detail': 'Not applicable.'}, status=status.HTTP_400_BAD_REQUEST)
    if not user.has_required_profile_fields():
        return Response({'detail': 'Complete your profile first.'}, status=status.HTTP_400_BAD_REQUEST)
    if not user.accounts.exists():
        return Response({'detail': 'Finish account setup first.'}, status=status.HTTP_400_BAD_REQUEST)
    if ProfileChangeRequest.objects.filter(user=user, status=ProfileChangeRequest.Status.PENDING).exists():
        return Response(
            {'detail': 'You already have a pending profile change request.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    serializer = ProfileChangeRequestCreateSerializer(data=request.data, context={'request': request})
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(
        {
            'detail': 'Your change request has been submitted for review. '
            'An administrator will apply your updates after approval.'
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def change_password(request):
    serializer = ChangePasswordSerializer(data=request.data, context={'request': request})
    serializer.is_valid(raise_exception=True)
    request.user.set_password(serializer.validated_data['new_password'])
    request.user.save()
    return Response({'detail': 'Password changed successfully.'})


@api_view(['POST'])
@permission_classes([AllowAny])
def password_reset_request(request):
    serializer = PasswordResetRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    email = serializer.validated_data['email']
    try:
        user = User.objects.get(email=email, is_active=True)
        token_str = secrets.token_urlsafe(48)
        PasswordResetToken.objects.create(
            user=user,
            token=token_str,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        send_email_notification.delay(
            user_id=str(user.id),
            event_type='password_reset',
            context={'token': token_str, 'full_name': user.full_name},
        )
    except User.DoesNotExist:
        pass
    return Response({'detail': 'If this email exists, a reset link has been sent.'})


@api_view(['POST'])
@permission_classes([AllowAny])
def password_reset_confirm(request):
    from .serializers import PasswordResetConfirmSerializer
    serializer = PasswordResetConfirmSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        reset_token = PasswordResetToken.objects.get(token=serializer.validated_data['token'])
        if not reset_token.is_valid():
            return Response({'detail': 'Token is invalid or expired.'}, status=status.HTTP_400_BAD_REQUEST)
        reset_token.user.set_password(serializer.validated_data['new_password'])
        reset_token.user.save()
        reset_token.is_used = True
        reset_token.save()
        return Response({'detail': 'Password reset successful.'})
    except PasswordResetToken.DoesNotExist:
        return Response({'detail': 'Invalid token.'}, status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def kyc_upload(request):
    serializer = KYCUploadSerializer(instance=request.user, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response({'detail': 'KYC document submitted for review.'})


@api_view(['POST'])
@permission_classes([CustomerAccessPermission])
def mfa_toggle(request):
    user = request.user
    user.is_mfa_enabled = not user.is_mfa_enabled
    user.save()
    status_text = 'enabled' if user.is_mfa_enabled else 'disabled'
    return Response({'detail': f'MFA {status_text}.'})


