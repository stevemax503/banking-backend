from pathlib import Path
from decouple import config, Csv
from datetime import timedelta

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _storage_path(env_key: str, default_relative: str) -> str:
    """
    Resolve STATIC_ROOT / MEDIA_ROOT.

    Docker-style .env files often set MEDIA_ROOT=/app/media. On the host that path is not
    writable. Only honor /app/... when USE_DOCKER_STORAGE=true.
    """
    raw = config(env_key, default='')
    use_docker_storage = config('USE_DOCKER_STORAGE', default=False, cast=bool)
    if not raw:
        return str(BASE_DIR / default_relative)
    p = Path(raw)
    if not p.is_absolute():
        return str(BASE_DIR / p)
    path_str = str(p)
    if path_str.startswith('/app') and not use_docker_storage:
        return str(BASE_DIR / default_relative)
    return path_str


SECRET_KEY = config('SECRET_KEY')
DEBUG = config('DEBUG', default=False, cast=bool)
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost', cast=Csv())

DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

THIRD_PARTY_APPS = [
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',
    'django_filters',
    'guardian',
    'axes',
    'django_otp',
    'django_otp.plugins.otp_totp',
    'django_otp.plugins.otp_email',
    'channels',
    'django_celery_beat',
    'drf_spectacular',
]

LOCAL_APPS = [
    'apps.users',
    'apps.accounts',
    'apps.cards',
    'apps.transactions',
    'apps.loans',
    'apps.statements',
    'apps.notifications',
    'apps.support',
    'apps.audit',
    'apps.admin_portal',
    'apps.payments',
    'apps.savings',
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django_otp.middleware.OTPMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'axes.middleware.AxesMiddleware',
    'apps.audit.middleware.AuditMiddleware',
    'apps.audit.middleware.CustomerActivityAuditMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': config('DB_NAME', default='banking_db'),
        'USER': config('DB_USER', default='postgres'),
        'PASSWORD': config('DB_PASSWORD', default=''),
        'HOST': config('DB_HOST', default='localhost'),
        'PORT': config('DB_PORT', default='5432'),
        'OPTIONS': {
            'connect_timeout': 10,
        },
        'CONN_MAX_AGE': 60,
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 8}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.Argon2PasswordHasher',
    'django.contrib.auth.hashers.PBKDF2PasswordHasher',
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = _storage_path('STATIC_ROOT', 'staticfiles')

MEDIA_URL = '/media/'
MEDIA_ROOT = _storage_path('MEDIA_ROOT', 'media')

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

AUTH_USER_MODEL = 'users.CustomUser'

AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',
    'django.contrib.auth.backends.ModelBackend',
    'guardian.backends.ObjectPermissionBackend',
]

# REST Framework
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '20/minute',
        'user': '100/minute',
        'auth': '5/minute',
        'transactions': '30/minute',
    },
}

# JWT
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=config('JWT_ACCESS_TOKEN_LIFETIME_MINUTES', default=15, cast=int)),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=config('JWT_REFRESH_TOKEN_LIFETIME_DAYS', default=7, cast=int)),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'ALGORITHM': 'HS256',
    'AUTH_HEADER_TYPES': ('Bearer',),
    'TOKEN_BLACKLIST_ENABLED': True,
}

# CORS — allow browser requests from the SPA origin(s).
# Set FRONTEND_URL and/or CORS_ALLOWED_ORIGINS (comma-separated). Both are merged.
_cors_origins = [
    o.strip()
    for o in (
        *config('FRONTEND_URL', default='', cast=Csv()),
        *config('CORS_ALLOWED_ORIGINS', default='', cast=Csv()),
    )
    if o and str(o).strip()
]
CORS_ALLOWED_ORIGINS = list(dict.fromkeys(_cors_origins)) or ['http://localhost:5173']
CORS_ALLOW_CREDENTIALS = True

# Channels
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            'hosts': [config('REDIS_URL', default='redis://localhost:6379/0')],
        },
    },
}

# Celery
CELERY_BROKER_URL = config('CELERY_BROKER_URL', default='amqp://guest:guest@localhost:5672//')
CELERY_RESULT_BACKEND = config('CELERY_RESULT_BACKEND', default='redis://localhost:6379/1')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'UTC'
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'
CELERY_TASK_ALWAYS_EAGER = config('CELERY_TASK_ALWAYS_EAGER', default=False, cast=bool)
CELERY_TASK_EAGER_PROPAGATES = True

# Cache
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': config('REDIS_URL', default='redis://localhost:6379/0'),
    }
}

# Email
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = config('EMAIL_HOST', default='smtp.mailgun.org')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_USE_SSL = config('EMAIL_USE_SSL', default=False, cast=bool)
INFO_EMAIL = config('INFO_EMAIL', default='info@safapaygroup.com')
SUPPORT_EMAIL = config('SUPPORT_EMAIL', default='support@safapaygroup.com')
SECURITY_EMAIL = config('SECURITY_EMAIL', default='security@safapaygroup.com')
ADMIN_EMAIL = config('ADMIN_EMAIL', default='admin@safapaygroup.com')
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default=f'SafaPay Bank <{INFO_EMAIL}>')
# Optional absolute URL for logo image in HTML emails (e.g. CDN or app static URL).
EMAIL_LOGO_URL = config('EMAIL_LOGO_URL', default='')
# Public HTTPS origin for email images, e.g. https://app.yourdomain.com (serves /email/*.png).
# Required for reliable images in Gmail when using localhost for FRONTEND_URL.
EMAIL_ASSETS_BASE_URL = config('EMAIL_ASSETS_BASE_URL', default='')
# Optional social links shown in the email footer (leave blank to hide icons).
EMAIL_SOCIAL_TWITTER = config('EMAIL_SOCIAL_TWITTER', default='')
EMAIL_SOCIAL_FACEBOOK = config('EMAIL_SOCIAL_FACEBOOK', default='')
EMAIL_SOCIAL_INSTAGRAM = config('EMAIL_SOCIAL_INSTAGRAM', default='')
EMAIL_SOCIAL_LINKEDIN = config('EMAIL_SOCIAL_LINKEDIN', default='')
EMAIL_SOCIAL_YOUTUBE = config('EMAIL_SOCIAL_YOUTUBE', default='')

# PDF statement letterhead (optional; shown in footer / header of generated PDFs)
STATEMENT_BANK_NAME = config('STATEMENT_BANK_NAME', default='bankApp')
STATEMENT_SUPPORT_PHONE = config('STATEMENT_SUPPORT_PHONE', default='')
STATEMENT_SUPPORT_EMAIL = config('STATEMENT_SUPPORT_EMAIL', default=INFO_EMAIL)
STATEMENT_BANK_ADDRESS = config('STATEMENT_BANK_ADDRESS', default='')
# Code shown on generated PDFs (amounts are numeric only; no symbol)
STATEMENT_DISPLAY_CURRENCY = config('STATEMENT_DISPLAY_CURRENCY', default='USD')

# OTP
OTP_EMAIL_TOKEN_VALIDITY = config('OTP_EMAIL_TOKEN_VALIDITY', default=300, cast=int)
OTP_EMAIL_SUBJECT = 'Your verification code'

# django-axes brute force protection
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = timedelta(minutes=30)
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_PARAMETERS = ['username', 'ip_address']

# django-guardian
ANONYMOUS_USER_NAME = None

# Sentry
SENTRY_DSN = config('SENTRY_DSN', default='')

# FX Rates
FX_RATES_API_KEY = config('FX_RATES_API_KEY', default='')
FX_RATES_BASE_URL = config('FX_RATES_BASE_URL', default='https://openexchangerates.org/api')

# KYC
ONFIDO_API_TOKEN = config('ONFIDO_API_TOKEN', default='')

# Encryption
FIELD_ENCRYPTION_KEY = config('FIELD_ENCRYPTION_KEY', default='')

# Security headers
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = 'DENY'

# Spectacular
SPECTACULAR_SETTINGS = {
    'TITLE': 'Banking API',
    'DESCRIPTION': 'REST API for the Banking Web Application',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'COMPONENT_SPLIT_REQUEST': True,
}

# Logging
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': str(BASE_DIR / 'logs' / 'app.log'),
            'maxBytes': 10 * 1024 * 1024,
            'backupCount': 5,
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console', 'file'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        'apps': {
            'handlers': ['console', 'file'],
            'level': 'DEBUG',
            'propagate': False,
        },
    },
}
