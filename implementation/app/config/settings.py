import os

from django.core.exceptions import ImproperlyConfigured

from app.config.database import postgres_database

# Django loads uppercase settings from the selected module.
from app.config.settings_base import *  # noqa: F403

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set.")

DATABASES = {"default": postgres_database("app", "DB_APP_PASSWORD")}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "request_errors": {"class": "logging.StreamHandler", "level": "ERROR"},
    },
    "loggers": {
        "django.request": {"handlers": ["request_errors"], "level": "ERROR"},
    },
}
