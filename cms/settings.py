"""
Django settings for the Collection Management System (CMS).

Configuration is layered (see cms/conf.py): environment variables (incl. .env)
override an optional config.ini in the project root, which overrides the code
defaults below. The same code therefore runs on SQLite (local dev, default) and
PostgreSQL (production) without changes — switch databases by setting
DB_ENGINE=postgres in the environment or in config.ini.
"""

from pathlib import Path

from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from a local .env file if present (before conf,
# because environment values take precedence over config.ini).
load_dotenv(BASE_DIR / '.env')

from . import conf

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = conf.get(
    'SECRET_KEY',
    'django-insecure-h#%@4x-7d*elx8=j83o8wh6jq%ob$&yzog$79_t$2*4z_sers0',
)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = conf.get_bool('DEBUG', True)

ALLOWED_HOSTS = conf.get_list('ALLOWED_HOSTS', 'localhost,127.0.0.1')

# e.g. CSRF_TRUSTED_ORIGINS=https://cms.example.com (needed for HTTPS behind a proxy)
CSRF_TRUSTED_ORIGINS = conf.get_list('CSRF_TRUSTED_ORIGINS')


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'accounts.apps.AccountsConfig',
    'Collection_Management_System.apps.CollectionManagementSystemConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # WhiteNoise serves static files efficiently in production (right after security).
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    # Default the UI to German: ignore the browser's Accept-Language header
    # unless the user picked a language explicitly (must run before
    # LocaleMiddleware, which would otherwise honour the header).
    'Collection_Management_System.middleware.DefaultLanguageMiddleware',
    'django.middleware.locale.LocaleMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    # Runtime-toggleable maintenance mode (needs request.user, so after auth).
    'Collection_Management_System.middleware.MaintenanceModeMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'cms.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                # Runtime settings as {{ site_config.<key> }} (lazy per lookup).
                'Collection_Management_System.context_processors.site_config',
            ],
        },
    },
]

WSGI_APPLICATION = 'cms.wsgi.application'


# Database — SQLite by default, PostgreSQL when DB_ENGINE=postgres.
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

if conf.get('DB_ENGINE', 'sqlite') == 'postgres':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': conf.get('DB_NAME', 'cms'),
            'USER': conf.get('DB_USER', 'cms'),
            'PASSWORD': conf.get('DB_PASSWORD', ''),
            'HOST': conf.get('DB_HOST', 'localhost'),
            'PORT': conf.get('DB_PORT', '5432'),
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }


# Custom user model — set before the first migration so it is the project default.
AUTH_USER_MODEL = 'accounts.User'

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'login'

# E-mail (password reset). Default: print mails to the console so local dev
# works without any setup. Point EMAIL_BACKEND at the SMTP backend + set the
# EMAIL_* variables in production.
EMAIL_BACKEND = conf.get(
    'EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend')
EMAIL_HOST = conf.get('EMAIL_HOST', 'localhost')
EMAIL_PORT = conf.get_int('EMAIL_PORT', 587)
EMAIL_HOST_USER = conf.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = conf.get('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = conf.get_bool('EMAIL_USE_TLS', True)
DEFAULT_FROM_EMAIL = conf.get('DEFAULT_FROM_EMAIL', 'cms@localhost')

# Map Django's message levels onto Bootstrap alert classes.
from django.contrib.messages import constants as message_constants

MESSAGE_TAGS = {
    message_constants.DEBUG: 'secondary',
    message_constants.INFO: 'info',
    message_constants.SUCCESS: 'success',
    message_constants.WARNING: 'warning',
    message_constants.ERROR: 'danger',
}


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

from django.utils.translation import gettext_lazy as _

LANGUAGE_CODE = 'de'

# Languages offered in the UI (language switcher). German is the source
# language: the msgids in the code/templates are German, English lives in
# locale/en/LC_MESSAGES. Add a tuple here + a .po file to support more.
LANGUAGES = [
    ('de', _('Deutsch')),
    ('en', _('Englisch')),
]

# Where Django looks for compiled translation catalogues.
LOCALE_PATHS = [BASE_DIR / 'locale']

TIME_ZONE = 'Europe/Berlin'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

# User-uploaded files (images, receipts, documents).
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Static file storage. In production (after `collectstatic`) enable hashed,
# compressed assets via WhiteNoise by setting STATIC_MANIFEST=True. Off by
# default so local dev and the test runner don't need a built manifest.
_static_backend = (
    'whitenoise.storage.CompressedManifestStaticFilesStorage'
    if conf.get_bool('STATIC_MANIFEST', False)
    else 'django.contrib.staticfiles.storage.StaticFilesStorage'
)
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': _static_backend},
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# Security hardening — all env-gated and OFF by default, so local dev and tests
# are unaffected. Enable these in production (see .env.production.example).
SECURE_SSL_REDIRECT = conf.get_bool('SECURE_SSL_REDIRECT', False)
SESSION_COOKIE_SECURE = conf.get_bool('SESSION_COOKIE_SECURE', False)
CSRF_COOKIE_SECURE = conf.get_bool('CSRF_COOKIE_SECURE', False)
SECURE_HSTS_SECONDS = conf.get_int('SECURE_HSTS_SECONDS', 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = conf.get_bool('SECURE_HSTS_INCLUDE_SUBDOMAINS', False)
SECURE_HSTS_PRELOAD = conf.get_bool('SECURE_HSTS_PRELOAD', False)
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'

# Trust the proxy's X-Forwarded-Proto header (set when terminating TLS upstream).
if conf.get_bool('USE_PROXY_SSL_HEADER', False):
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
