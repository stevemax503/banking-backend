# Environment Variables

Copy `.env.example` to `.env` and fill in every value before starting the application.

---

## Django Core

| Variable | Example | Description |
|---|---|---|
| `SECRET_KEY` | `django-insecure-...` | Django secret key. Generate with `python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"` |
| `DEBUG` | `False` | Never `True` in production |
| `ALLOWED_HOSTS` | `api.yourdomain.com,localhost` | Comma-separated list of valid host headers |
| `DJANGO_SETTINGS_MODULE` | `config.settings.prod` | Settings module to use |

---

## Database (PostgreSQL)

| Variable | Example | Description |
|---|---|---|
| `DB_NAME` | `bankapp` | Database name |
| `DB_USER` | `bankapp_user` | Database user |
| `DB_PASSWORD` | `strong_password_here` | Database password |
| `DB_HOST` | `postgres` | Service name (or IP) |
| `DB_PORT` | `5432` | PostgreSQL port |

---

## Redis

| Variable | Example | Description |
|---|---|---|
| `REDIS_URL` | `redis://redis:6379/0` | Used for cache and Channels layer |

---

## RabbitMQ (Celery Broker)

| Variable | Example | Description |
|---|---|---|
| `RABBITMQ_URL` | `amqp://guest:guest@rabbitmq:5672/` | Celery broker URL |

---

## Email (SMTP)

| Variable | Example | Description |
|---|---|---|
| `EMAIL_BACKEND` | `django.core.mail.backends.smtp.EmailBackend` | In dev: console only when `EMAIL_HOST_PASSWORD` is unset, or `USE_CONSOLE_EMAIL_IN_DEV=true` |
| `USE_CONSOLE_EMAIL_IN_DEV` | (auto) | `true` = print mail to server logs instead of SMTP; defaults to `false` when SMTP password is set |
| `EMAIL_HOST` | `smtp.mailgun.org` | SMTP server hostname |
| `EMAIL_PORT` | `587` | SMTP port (587 = STARTTLS, 465 = SSL) |
| `EMAIL_USE_TLS` | `True` | Enable TLS |
| `EMAIL_HOST_USER` | `no-reply@yourdomain.com` | SMTP username |
| `EMAIL_HOST_PASSWORD` | `...` | SMTP password or API key |
| `DEFAULT_FROM_EMAIL` | `SafaPay Bank <info@safapaygroup.com>` | Fallback From when no event-specific alias applies |
| `INFO_EMAIL` | `info@safapaygroup.com` | Welcome, deposits/credits, loans, compliance confirmations |
| `SUPPORT_EMAIL` | `support@safapaygroup.com` | Support ticket replies; footer contact on informational mail |
| `SECURITY_EMAIL` | `security@safapaygroup.com` | OTP, password reset, debit/withdrawal alerts |
| `ADMIN_EMAIL` | `admin@safapaygroup.com` | Admin-approved profile updates |

All four addresses should be configured as **send-as aliases** of the primary SMTP mailbox (e.g. Google Workspace aliases of `info@safapaygroup.com`).

---

## Frontend

| Variable | Example | Description |
|---|---|---|
| `FRONTEND_URL` | `https://app.yourdomain.com` | Used to build password reset links in emails |
| `CORS_ALLOWED_ORIGINS` | `https://app.yourdomain.com` | Vercel frontend origin |

---

## JWT

| Variable | Example | Description |
|---|---|---|
| `JWT_ACCESS_TOKEN_LIFETIME_MINUTES` | `15` | Access token TTL |
| `JWT_REFRESH_TOKEN_LIFETIME_DAYS` | `7` | Refresh token TTL |

---

## MFA / OTP

| Variable | Example | Description |
|---|---|---|
| `OTP_EMAIL_TOKEN_VALIDITY` | `600` | Email OTP validity in seconds (default: 10 min) |
| `REQUIRE_MFA_FOR_ADMINS` | `True` | Force MFA for all non-CUSTOMER roles |

---

## Media Storage

| Variable | Example | Description |
|---|---|---|
| `MEDIA_ROOT` | `/app/media` | Absolute path to media directory on server. If you use a Docker-style `/app/...` layout, you may need `USE_DOCKER_STORAGE=true`. Otherwise omit this so files go under the project `media/` folder. |
| `MEDIA_URL` | `/media/` | URL prefix for media files |
| `STATIC_ROOT` | `/app/staticfiles` | Same pattern as `MEDIA_ROOT` for collected static files |
| `USE_DOCKER_STORAGE` | `true` | Legacy switch for Docker-style `/app/...` paths. When unset/false, `/app/...` paths are ignored and `BASE_DIR/media` and `BASE_DIR/staticfiles` are used. |

---

## Encryption

| Variable | Example | Description |
|---|---|---|
| `FIELD_ENCRYPTION_KEY` | `base64-encoded-32-bytes` | Key for `django-encrypted-model-fields`. **Never rotate without migrating data.** Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

---

## External APIs

| Variable | Example | Description |
|---|---|---|
| `FX_RATES_API_URL` | `https://openexchangerates.org/api/latest.json` | FX rates endpoint |
| `FX_RATES_API_KEY` | `...` | Open Exchange Rates App ID |
| `FX_BASE_CURRENCY` | `USD` | All rates are relative to this currency |
| `ONFIDO_API_TOKEN` | `api_live_...` | Trulioo/Onfido KYC API token |

---

## Error Tracking

| Variable | Example | Description |
|---|---|---|
| `SENTRY_DSN` | `https://...@sentry.io/...` | Leave blank to disable Sentry |

---

## Security Headers (Production)

These are set automatically in `config/settings/prod.py` — no env variables needed:

- `SECURE_SSL_REDIRECT = True`
- `SECURE_HSTS_SECONDS = 31536000`
- `SECURE_HSTS_INCLUDE_SUBDOMAINS = True`
- `SESSION_COOKIE_SECURE = True`
- `CSRF_COOKIE_SECURE = True`
