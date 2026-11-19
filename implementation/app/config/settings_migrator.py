from app.config.database import postgres_database

# Django loads uppercase settings from the selected module.
from app.config.settings_base import *  # noqa: F403

DATABASES = {"default": postgres_database("migrator", "DB_MIGRATOR_PASSWORD")}
