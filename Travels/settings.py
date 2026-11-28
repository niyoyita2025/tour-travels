"""
Django settings for the Travels project (Explore Rwanda tour website).

Sensitive values are read from environment variables. See .env.example.
"""

import os
import sys
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ('1', 'true', 'yes', 'on')


def env_list(name, default=''):
    return [item.strip() for item in os.environ.get(name, default).split(',') if item.strip()]


def load_dotenv(path):
    """Read KEY=VALUE lines from a .env file into os.environ. Variables already set win."""
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


# Local settings (including the Gmail app password) live in Travels/.env, which is never committed.
load_dotenv(BASE_DIR / '.env')

DEBUG = env_bool('DJANGO_DEBUG', True)

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured('Set DJANGO_SECRET_KEY when DJANGO_DEBUG is off.')
    # Development-only key. Never used when DJANGO_DEBUG is off.
    SECRET_KEY = 'dev-only-key-change-me-for-production-use-env-var'

ALLOWED_HOSTS = env_list('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1]')
CSRF_TRUSTED_ORIGINS = env_list('DJANGO_CSRF_TRUSTED_ORIGINS')

# Render sets these automatically, so the site works on its .onrender.com address without extra config.
RENDER_EXTERNAL_HOSTNAME = os.environ.get('RENDER_EXTERNAL_HOSTNAME', '').strip()
if RENDER_EXTERNAL_HOSTNAME:
    ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)
    CSRF_TRUSTED_ORIGINS.append(f'https://{RENDER_EXTERNAL_HOSTNAME}')


INSTALLED_APPS = [
    # Listed first so its createsuperuser command (email required and unique) replaces Django's.
    # None of its template or static file names overlap with django.contrib.admin's.
    'Tour_travels',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Sends due booking reminder emails at most every 15 minutes while the site gets visits.
    'Tour_travels.reminders.ReminderMiddleware',
]
# Off while the test suite runs, so reminders never appear in unrelated tests' mail.
BOOKING_REMINDERS_ON_VISIT = env_bool('BOOKING_REMINDERS_ON_VISIT', 'test' not in sys.argv)

ROOT_URLCONF = 'Travels.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'Tour_travels.context_processors.site_settings',
            ],
        },
    },
]

WSGI_APPLICATION = 'Travels.wsgi.application'


# Production uses PostgreSQL. Set DATABASE_URL to the Postgres connection string (on Render, the
# database's Internal Database URL). Local development falls back to SQLite when it is not set.
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
if DATABASE_URL:
    DATABASES = {
        'default': dj_database_url.parse(DATABASE_URL, conn_max_age=600, conn_health_checks=True),
    }
else:
    if not DEBUG:
        # SQLite lives on the server's disk, which Render erases on every deploy. Refuse to run without Postgres.
        raise ImproperlyConfigured('Set DATABASE_URL when DJANGO_DEBUG is off. SQLite is for local development only.')
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }


# Argon2 is preferred. Passwords stored with the older PBKDF2 hasher still sign in, and Django moves them
# to Argon2 the next time their owner signs in.
PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.Argon2PasswordHasher',
    'django.contrib.auth.hashers.PBKDF2PasswordHasher',
    'django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher',
]

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
     'OPTIONS': {'min_length': 8}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'home'
# Sign in with a username or with the email address the account was registered with.
AUTHENTICATION_BACKENDS = ['Tour_travels.backends.EmailOrUsernameBackend']
# Password reset links sent by administrators (and their tokens) expire after this many minutes.
PASSWORD_RESET_TIMEOUT = 60 * int(os.environ.get('PASSWORD_RESET_MINUTES', '60'))

# Six-digit email codes for sign-up verification and forgot-password.
OTP_LIFETIME_SECONDS = 5 * 60            # a code expires after 5 minutes
OTP_RESEND_SECONDS = 60                  # wait this long before a new code can be requested
OTP_MAX_ATTEMPTS = 5                     # wrong entries before a code is locked
PASSWORD_RESET_CHOOSE_SECONDS = 10 * 60  # time to choose a new password once the code is accepted
# Codes emailed to one account for one purpose, across all devices. With OTP_MAX_ATTEMPTS (5 per code),
# this caps guesses at 25 per account per hour, however many devices or addresses are used.
OTP_ACCOUNT_MAX_SENDS = 5
OTP_ACCOUNT_SEND_WINDOW_SECONDS = 60 * 60


LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Kigali'
USE_I18N = True
USE_TZ = True


STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage'},
}

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'
# Serve uploaded files from Django when DEBUG is off (Render has no separate file server).
# Note: on Render's free plan the disk is reset on every deploy, so uploads must be re-added afterwards.
SERVE_MEDIA = env_bool('DJANGO_SERVE_MEDIA', True)
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024


EMAIL_BACKEND = os.environ.get(
    'DJANGO_EMAIL_BACKEND',
    # Local development prints emails to the terminal. Anywhere else, codes must not reach the logs.
    'django.core.mail.backends.console.EmailBackend' if DEBUG
    else 'django.core.mail.backends.smtp.EmailBackend',
)
# Gmail SMTP. EMAIL_HOST_PASSWORD must be a Google "app password", never the normal Gmail password.
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
# Google shows app passwords in groups of four with spaces. Spaces are removed, so either form works.
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '').replace(' ', '')
EMAIL_USE_TLS = env_bool('EMAIL_USE_TLS', True)
EMAIL_TIMEOUT = int(os.environ.get('EMAIL_TIMEOUT', '20'))  # seconds before a send is reported as failed
# Gmail rewrites the sender unless it is the account that signs in, so the sender follows EMAIL_HOST_USER by default.
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL') or (
    f'Explore Rwanda <{EMAIL_HOST_USER}>' if EMAIL_HOST_USER else 'Explore Rwanda <noreply@example.com>'
)
SITE_NAME = 'Explore Rwanda with a Local Guide'
SITE_URL = os.environ.get('SITE_URL') or os.environ.get('RENDER_EXTERNAL_URL') or 'http://127.0.0.1:8000'

# Booking calendar policy
BOOKING_HOLD_HOURS = int(os.environ.get('BOOKING_HOLD_HOURS', '48'))  # pending requests keep seats this long
CANCELLATION_NOTICE_HOURS = int(os.environ.get('CANCELLATION_NOTICE_HOURS', '48'))  # cancel up to this long before a tour
LIMITED_SEATS_THRESHOLD = int(os.environ.get('LIMITED_SEATS_THRESHOLD', '3'))  # yellow when this few seats remain

# Each destination gets a bookable tour automatically. These set how that tour starts.
AUTO_TOUR_PRICE = os.environ.get('AUTO_TOUR_PRICE', '20.00')  # USD per guest; edit the tour afterwards if needed
AUTO_TOUR_MAX_GUESTS = int(os.environ.get('AUTO_TOUR_MAX_GUESTS', '8'))
AUTO_TOUR_CAPACITY = int(os.environ.get('AUTO_TOUR_CAPACITY', '8'))  # seats per open date
AUTO_TOUR_DAYS_AHEAD = int(os.environ.get('AUTO_TOUR_DAYS_AHEAD', '30'))  # open dates kept this many days ahead
AUTO_TOUR_START_TIME = os.environ.get('AUTO_TOUR_START_TIME', '09:00')
# Contact details, WhatsApp number, payment instructions and the staff
# notification email are managed in the staff dashboard (Website content).


if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = 'same-origin'
    X_FRAME_OPTIONS = 'DENY'
    # Render (and most hosts) terminate HTTPS in front of Django and forward the original scheme in this header.
    # Without it, Django sees plain HTTP and the SSL redirect below would loop forever.
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    # Set DJANGO_SECURE_SSL_REDIRECT=1 in production once HTTPS is in place.
    SECURE_SSL_REDIRECT = env_bool('DJANGO_SECURE_SSL_REDIRECT', False)
    SECURE_HSTS_SECONDS = int(os.environ.get('DJANGO_HSTS_SECONDS', '0'))

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
