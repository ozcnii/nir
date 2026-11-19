import os

from django.core.exceptions import ImproperlyConfigured


def postgres_database(role: str, password_variable: str) -> dict[str, object]:
    name = os.environ.get("DB_NAME")
    if not name:
        raise ImproperlyConfigured("DB_NAME must be set.")

    password = os.environ.get(password_variable)
    if not password:
        raise ImproperlyConfigured(f"{password_variable} must be set.")

    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": name,
        "USER": role,
        "PASSWORD": password,
        "HOST": os.environ.get("DB_HOST", "db"),
        "OPTIONS": {"connect_timeout": 3},
        "PORT": os.environ.get("DB_PORT", "5432"),
    }
