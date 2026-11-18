from config.database import postgres_database
from config.settings_base import *  # noqa: F403

DATABASES = {"default": postgres_database("migrator", "DB_MIGRATOR_PASSWORD")}
