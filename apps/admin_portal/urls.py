from django.urls import path

from apps.payments import views as payment_admin_views

from . import views

urlpatterns = [
    # Dashboard
    path('dashboard/', views.admin_dashboard, name='admin-dashboard'),

    # Users
    path('users/', views.AdminUserListView.as_view(), name='admin-user-list'),
    path('users/create-staff/', views.create_staff_user, name='admin-user-create-staff'),
    path('users/<uuid:pk>/', views.AdminUserDetailView.as_view(), name='admin-user-detail'),
    path('users/<uuid:pk>/role/', views.change_user_role, name='admin-user-role'),
    path('users/<uuid:pk>/lock/', views.toggle_user_lock, name='admin-user-lock'),
    path('users/<uuid:pk>/kyc/', views.approve_kyc, name='admin-user-kyc'),
    path('users/<uuid:pk>/issue-login-otp/', views.admin_issue_login_otp, name='admin-issue-login-otp'),
    path('users/<uuid:pk>/impersonate/', views.admin_impersonate_customer, name='admin-impersonate-customer'),
    path('email-otps/', views.AdminEmailOTPListView.as_view(), name='admin-email-otp-list'),
    path('profile-change-requests/', views.AdminProfileChangeRequestListView.as_view(), name='admin-profile-requests'),
    path(
        'profile-change-requests/<uuid:pk>/approve/',
        views.approve_profile_change_request,
        name='admin-profile-request-approve',
    ),
    path(
        'profile-change-requests/<uuid:pk>/reject/',
        views.reject_profile_change_request,
        name='admin-profile-request-reject',
    ),

    # Accounts
    path('accounts/', views.AdminAccountListView.as_view(), name='admin-account-list'),
    path('accounts/<uuid:pk>/status/', views.admin_account_status, name='admin-account-status'),
    path('accounts/<uuid:pk>/adjust/', views.admin_adjust_balance, name='admin-account-adjust'),
    path('accounts/<uuid:pk>/deposit/', views.admin_account_deposit, name='admin-account-deposit'),
    path('accounts/<uuid:pk>/debit/', views.admin_account_debit, name='admin-account-debit'),
    path('deposit-preview/', views.admin_deposit_preview, name='admin-deposit-preview'),

    # Transactions
    path('transactions/', views.AdminTransactionListView.as_view(), name='admin-transaction-list'),
    path('transactions/bulk-delete/', views.admin_bulk_delete_transactions, name='admin-transaction-bulk-delete'),
    path('transactions/<uuid:pk>/', views.AdminTransactionDetailView.as_view(), name='admin-transaction-detail'),
    path('transactions/<uuid:pk>/update/', views.admin_update_transaction_view, name='admin-transaction-update'),
    path('transactions/<uuid:pk>/delete/', views.admin_delete_transaction_view, name='admin-transaction-delete'),
    path('transactions/<uuid:pk>/reverse/', views.admin_reverse_transaction, name='admin-transaction-reverse'),
    path('transactions/<uuid:pk>/flag/', views.flag_transaction, name='admin-transaction-flag'),

    # Loans
    path('loans/', views.AdminLoanApplicationListView.as_view(), name='admin-loan-list'),
    path('loans/<uuid:pk>/review/', views.review_loan, name='admin-loan-review'),
    path('loans/<uuid:pk>/disburse/', views.disburse_loan_view, name='admin-loan-disburse'),
    path('loan-products/', views.AdminLoanProductListCreateView.as_view(), name='admin-loan-product-list'),
    path('loan-products/<uuid:pk>/', views.AdminLoanProductDetailView.as_view(), name='admin-loan-product-detail'),

    # Card products (per account type)
    path('card-products/', views.AdminCardProductListView.as_view(), name='admin-card-product-list'),
    path('card-products/<uuid:pk>/', views.AdminCardProductDetailView.as_view(), name='admin-card-product-detail'),

    # Fees
    path('fees/', views.AdminFeeListView.as_view(), name='admin-fee-list'),
    path('fees/<int:pk>/', views.AdminFeeDetailView.as_view(), name='admin-fee-detail'),
    path('compliance-fee-lines/', views.AdminComplianceFeeLineListView.as_view(), name='admin-compliance-fee-line-list'),
    path('compliance-fee-lines/<uuid:pk>/', views.AdminComplianceFeeLineDetailView.as_view(), name='admin-compliance-fee-line-detail'),
    path(
        'pending-compliance-sessions/',
        views.admin_pending_compliance_sessions,
        name='admin-pending-compliance-sessions',
    ),
    path(
        'pending-compliance-sessions/<uuid:session_id>/',
        views.admin_pending_compliance_session_delete,
        name='admin-pending-compliance-session-delete',
    ),
    path(
        'pending-compliance-sessions/<uuid:session_id>/lines/<uuid:line_id>/confirm-payment/',
        views.admin_regulated_line_confirm_payment,
        name='admin-regulated-line-confirm-payment',
    ),
    path(
        'pending-compliance-sessions/<uuid:session_id>/lines/<uuid:line_id>/charge-send-otp/',
        views.admin_regulated_line_charge_send_otp,
        name='admin-regulated-line-charge-send-otp',
    ),
    path(
        'pending-compliance-sessions/<uuid:session_id>/lines/<uuid:line_id>/allow-customer-charge/',
        views.admin_regulated_line_allow_customer_charge,
        name='admin-regulated-line-allow-customer-charge',
    ),
    path('exchange-rates/', views.AdminExchangeRateListView.as_view(), name='admin-exchange-rate-list'),
    path('exchange-rates/<int:pk>/', views.AdminExchangeRateDetailView.as_view(), name='admin-exchange-rate-detail'),

    # Bill payment management fees
    path(
        'payment-fees/settings/',
        payment_admin_views.AdminPaymentFeeSettingsView.as_view(),
        name='admin-payment-fee-settings',
    ),
    path(
        'payment-fees/overrides/',
        payment_admin_views.AdminPaymentFeeOverrideListCreateView.as_view(),
        name='admin-payment-fee-override-list',
    ),
    path(
        'payment-fees/overrides/<uuid:pk>/',
        payment_admin_views.AdminPaymentFeeOverrideDetailView.as_view(),
        name='admin-payment-fee-override-detail',
    ),

    # Support
    path('tickets/', views.AdminTicketListView.as_view(), name='admin-ticket-list'),
    path('tickets/<uuid:pk>/reply/', views.admin_ticket_reply, name='admin-ticket-reply'),
    path('tickets/<uuid:pk>/status/', views.admin_update_ticket_status, name='admin-ticket-status'),

    # Audit
    path('audit-logs/', views.AuditLogListView.as_view(), name='admin-audit-logs'),
]
