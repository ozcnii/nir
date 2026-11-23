import os

from app.config.database import postgres_database

# Django loads uppercase settings from the selected module.
from app.config.settings_base import *  # noqa: F403

SECRET_KEY = "local-schema-tests"
DATABASES = {"default": postgres_database("postgres", "POSTGRES_PASSWORD")}
CATALOG_DEMO_PREFIX = (
    "ui-multi-source-" if os.environ.get("CATALOG_PREVIEW") == "1" else ""
)
