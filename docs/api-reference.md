# API Reference

Base URL: `https://api.yourdomain.com` (production) · `http://localhost:8000` (dev)

All endpoints except registration and login require:
```
Authorization: Bearer <access_token>
Content-Type: application/json
```

Interactive docs available at `/api/docs/` (Swagger UI) and `/api/redoc/`.

---

## Authentication — `/api/auth/`

### Register
`POST /api/auth/register/`

```json
// Request
{
  "email": "customer@example.com",
  "full_name": "Legal Full Name",
  "phone": "+1234567890",
  "password": "SecurePass123!",
  "password_confirm": "SecurePass123!"
}

// Response 201
{ "detail": "Registration successful. Please verify your email." }
```

---

### Login
`POST /api/auth/login/`

```json
// Request
{ "email": "customer@example.com", "password": "SecurePass123!" }

// Response 200 (MFA disabled)
{ "access": "<jwt_token>", "refresh": "<refresh_token>" }

// Response 200 (MFA enabled)
{ "mfa_required": true, "detail": "MFA verification required." }
```

---

### MFA Verify
`POST /api/auth/login/mfa/`

```json
// Request
{
  "email": "customer@example.com",
  "token": "123456",
  "mfa_type": "email"   // or "totp"
}

// Response 200
{ "access": "<jwt_token>", "refresh": "<refresh_token>" }
```

---

### Refresh Token
`POST /api/auth/token/refresh/`

```json
// Request
{ "refresh": "<refresh_token>" }

// Response 200
{ "access": "<new_access_token>", "refresh": "<new_refresh_token>" }
```

---

### Logout
`POST /api/auth/logout/`

```json
// Request
{ "refresh": "<refresh_token>" }

// Response 200
{ "detail": "Successfully logged out." }
```

---

### Get Profile
`GET /api/auth/profile/`

```json
// Response 200
{
  "id": "uuid",
  "email": "customer@example.com",
  "full_name": "Legal Full Name",
  "phone": "+1234567890",
  "role": "CUSTOMER",
  "kyc_status": "PENDING",
  "is_mfa_enabled": false,
  "date_joined": "2024-01-15T10:00:00Z"
}
```

---

### Update Profile
`PATCH /api/auth/profile/`

```json
// Request (all fields optional)
{ "full_name": "Updated Legal Name", "phone": "+19876543210", "address": "123 Main St" }
```

---

### Change Password
`POST /api/auth/change-password/`

```json
// Request
{
  "current_password": "OldPass123!",
  "new_password": "NewPass456!",
  "new_password_confirm": "NewPass456!"
}

// Response 200
{ "detail": "Password changed successfully." }
```

---

### Request Password Reset
`POST /api/auth/password-reset/`

```json
// Request
{ "email": "customer@example.com" }

// Response 200 (always, even if email not found — prevents enumeration)
{ "detail": "If this email exists, a reset link has been sent." }
```

---

### Confirm Password Reset
`POST /api/auth/password-reset/confirm/`

```json
// Request
{
  "token": "<token_from_email>",
  "new_password": "NewPass456!",
  "new_password_confirm": "NewPass456!"
}

// Response 200
{ "detail": "Password reset successful." }
```

---

### Upload KYC Document
`POST /api/auth/kyc/upload/`  
Content-Type: `multipart/form-data`

```
kyc_document: <file>
```

---

### Toggle MFA
`POST /api/auth/mfa/toggle/`

```json
// Response 200
{ "detail": "MFA enabled." }
```

---

## Accounts — `/api/accounts/`

### List Accounts
`GET /api/accounts/`

Query params: `account_type`, `status`, `currency`, `search`, `ordering`

```json
// Response 200
{
  "count": 2,
  "results": [
    {
      "id": "uuid",
      "account_number": "1234567890",
      "account_type": "CHECKING",
      "currency": { "code": "USD", "name": "US Dollar", "symbol": "$" },
      "balance": "4820.50",
      "available_balance": "4820.50",
      "status": "ACTIVE",
      "nickname": "Main Account",
      "created_at": "2024-01-15T10:00:00Z"
    }
  ]
}
```

---

### Open Account
`POST /api/accounts/`

```json
// Request
{
  "account_type": "SAVINGS",
  "currency_id": 1,
  "nickname": "Rainy Day Fund"
}

// Response 201 — AccountSerializer
```

---

### Get Account Detail
`GET /api/accounts/{id}/`

---

### List Currencies
`GET /api/accounts/currencies/`

```json
// Response 200
[
  { "id": 1, "code": "USD", "name": "US Dollar", "symbol": "$" },
  { "id": 2, "code": "EUR", "name": "Euro", "symbol": "€" }
]
```

---

## Transactions — `/api/transactions/`

### List Transactions
`GET /api/transactions/`

Query params: `transaction_type`, `status`, `currency`, `start_date`, `end_date`, `min_amount`, `max_amount`, `search`

---

### Get Transaction Detail
`GET /api/transactions/{id}/`

```json
// Response 200
{
  "id": "uuid",
  "reference_number": "TXN1234567890",
  "transaction_type": "TRANSFER_INTERNAL",
  "amount": "250.00",
  "currency": "USD",
  "from_account": "uuid",
  "from_account_number": "1234567890",
  "to_account": "uuid",
  "to_account_number": "0987654321",
  "status": "COMPLETED",
  "description": "Rent payment",
  "fee_amount": "0.50",
  "exchange_rate": "1.00000000",
  "created_at": "2024-01-15T10:00:00Z",
  "completed_at": "2024-01-15T10:00:01Z"
}
```

---

### Deposit
`POST /api/transactions/deposit/`

```json
// Request
{
  "account_id": "uuid",
  "amount": "500.00",
  "description": "Salary deposit",
  "idempotency_key": "unique-client-key-123"
}

// Response 201 — TransactionSerializer
```

---

### Withdraw
`POST /api/transactions/withdraw/`

```json
// Request
{
  "account_id": "uuid",
  "amount": "100.00",
  "description": "ATM withdrawal",
  "idempotency_key": "unique-client-key-456"
}

// Response 201 — TransactionSerializer
// Error 400: { "detail": "Insufficient available balance." }
```

---

### Transfer
`POST /api/transactions/transfer/`

```json
// Request
{
  "from_account_id": "uuid",
  "to_account_id": "uuid",
  "amount": "250.00",
  "description": "Rent payment",
  "transfer_type": "TRANSFER_INTERNAL",
  "idempotency_key": "unique-client-key-789"
}

// transfer_type options:
//   TRANSFER_INTERNAL      — between own accounts (no fee)
//   TRANSFER_EXTERNAL      — to another customer's account (small fee)
//   TRANSFER_INTERNATIONAL — cross-currency (FX rate applied, higher fee)

// Response 201 — TransactionSerializer
```

---

### List Fees
`GET /api/transactions/fees/`

---

### List Exchange Rates
`GET /api/transactions/exchange-rates/`

---

## Loans — `/api/loans/`

### List Loan Products
`GET /api/loans/products/`

### Apply for Loan
`POST /api/loans/applications/`

```json
// Request
{
  "product": "uuid",
  "requested_amount": "15000.00",
  "term_months": 36,
  "purpose": "Home renovation"
}
```

### List My Applications
`GET /api/loans/applications/`

### Get Application Detail
`GET /api/loans/applications/{id}/`

### List My Loan Accounts
`GET /api/loans/accounts/`

### Get Loan Account + Schedule
`GET /api/loans/accounts/{id}/`

### Make Loan Payment
`POST /api/loans/payment/`

```json
// Request
{
  "loan_account_id": "uuid",
  "account_id": "uuid",
  "amount": "416.67"
}

// Response 200
{ "detail": "Payment successful.", "reference": "TXN1234567890" }
```

---

## Statements — `/api/statements/`

### List Statements
`GET /api/statements/`

### Request Statement Generation
`POST /api/statements/request/`

```json
// Request
{
  "account_id": "uuid",
  "period_start": "2024-01-01",
  "period_end": "2024-01-31"
}

// Response 200
{ "detail": "Statement generation started. You will be notified when ready." }
```

### Download Statement PDF
`GET /api/statements/{id}/download/`

Returns: `application/pdf` binary

---

## Notifications — `/api/notifications/`

### List Notifications
`GET /api/notifications/`

### Mark One Read
`PATCH /api/notifications/{id}/read/`

### Mark All Read
`POST /api/notifications/read-all/`

### Get/Update Preferences
`GET /api/notifications/preferences/`  
`PATCH /api/notifications/preferences/`

```json
// Preferences body
{
  "transaction_alerts": true,
  "low_balance_alerts": true,
  "low_balance_threshold": "100.00",
  "loan_alerts": true,
  "security_alerts": true,
  "statement_alerts": true
}
```

---

## Support — `/api/support/`

### List Tickets
`GET /api/support/`

### Create Ticket
`POST /api/support/`

```json
// Request
{
  "subject": "Transaction not reflected",
  "priority": "HIGH",
  "initial_message": "I made a deposit 2 hours ago but my balance hasn't updated.",
  "related_transaction": "uuid"  // optional
}
```

### Get Ticket + Messages
`GET /api/support/{id}/`

### Add Message
`POST /api/support/{id}/message/`

```json
{ "body": "Here is a screenshot of the transaction." }
```

### Close Ticket
`POST /api/support/{id}/close/`

---

## Admin Portal — `/api/admin-portal/`

> All endpoints require a non-CUSTOMER role. Role requirements vary per endpoint.

### Dashboard Stats
`GET /api/admin-portal/dashboard/`  **[Any Admin]**

```json
{
  "total_users": 1420,
  "total_accounts": 2100,
  "active_accounts": 1987,
  "total_transactions_this_month": 8432,
  "transaction_volume_this_month": "4820500.00",
  "pending_loan_applications": 12,
  "open_support_tickets": 34,
  "flagged_transactions": 3
}
```

### Users
| Method | Endpoint | Role |
|---|---|---|
| GET | `/admin-portal/users/` | Any Admin |
| GET/PATCH | `/admin-portal/users/{id}/` | Any Admin |
| POST | `/admin-portal/users/{id}/role/` | SUPER_ADMIN |
| POST | `/admin-portal/users/{id}/lock/` | Any Admin |
| POST | `/admin-portal/users/{id}/kyc/` | Any Admin |

### Accounts
| Method | Endpoint | Role |
|---|---|---|
| GET | `/admin-portal/accounts/` | Any Admin |
| POST | `/admin-portal/accounts/{id}/status/` | OPERATIONS_TELLER |
| POST | `/admin-portal/accounts/{id}/adjust/` | OPERATIONS_TELLER |

### Transactions
| Method | Endpoint | Role |
|---|---|---|
| GET | `/admin-portal/transactions/` | Any Admin |
| POST | `/admin-portal/transactions/{id}/reverse/` | OPERATIONS_TELLER |
| POST | `/admin-portal/transactions/{id}/flag/` | Any Admin |

### Loans
| Method | Endpoint | Role |
|---|---|---|
| GET | `/admin-portal/loans/` | LOAN_OFFICER |
| POST | `/admin-portal/loans/{id}/review/` | LOAN_OFFICER |
| POST | `/admin-portal/loans/{id}/disburse/` | LOAN_OFFICER |

### Fees & Rates
| Method | Endpoint | Role |
|---|---|---|
| GET/POST | `/admin-portal/fees/` | SUPER_ADMIN |
| GET/PATCH | `/admin-portal/fees/{id}/` | SUPER_ADMIN |
| GET/POST | `/admin-portal/exchange-rates/` | SUPER_ADMIN |
| GET/PATCH | `/admin-portal/exchange-rates/{id}/` | SUPER_ADMIN |

### Audit Logs
`GET /api/admin-portal/audit-logs/`  **[COMPLIANCE_AUDITOR]**

Query params: `action`, `target_model`, `search`

---

## WebSocket

`wss://api.yourdomain.com/ws/updates/?token=<access_token>`

### Incoming Messages (server → client)

```json
// New notification
{
  "type": "notification",
  "notification": {
    "id": "uuid",
    "event_type": "TRANSACTION",
    "subject": "Transaction Alert",
    "body": "...",
    "is_read": false,
    "sent_at": "2024-01-15T10:00:00Z"
  }
}

// Balance update
{
  "type": "balance_update",
  "account_id": "uuid",
  "balance": "4820.50"
}
```

### Outgoing Messages (client → server)

```json
// Mark all notifications read
{ "type": "mark_read" }
```

---

## Error Responses

All errors follow this structure:

```json
// 400 Bad Request
{ "field_name": ["Error message."] }
{ "detail": "Human readable error." }

// 401 Unauthorized
{ "detail": "Authentication credentials were not provided." }

// 403 Forbidden
{ "detail": "You do not have permission to perform this action." }

// 404 Not Found
{ "detail": "Not found." }

// 429 Too Many Requests
{ "detail": "Request was throttled. Expected available in 52 seconds." }
```

---

## Pagination

All list endpoints return paginated responses:

```json
{
  "count": 150,
  "next": "http://localhost:8000/api/transactions/?page=2",
  "previous": null,
  "results": [...]
}
```

Default page size: **20**. Override with `?page_size=50` (max 100).
