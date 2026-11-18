from config.database import postgres_database

# Django loads uppercase settings from the selected module.
from config.settings_base import *  # noqa: F403

SECRET_KEY = "local-schema-tests"
DATABASES = {"default": postgres_database("postgres", "POSTGRES_PASSWORD")}
