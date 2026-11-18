import os

from django.core.exceptions import ImproperlyConfigured

from config.database import postgres_database

# Django loads uppercase settings from the selected module.
from config.settings_base import *  # noqa: F403

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set.")

DATABASES = {"default": postgres_database("app", "DB_APP_PASSWORD")}
