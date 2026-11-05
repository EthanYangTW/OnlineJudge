import os

os.environ.setdefault("OJ_SECRET_KEY", "django-test-key-only-never-use-in-production")
os.environ.setdefault("FCU_CLASSROOM_ONLY", "true")

from oj.settings import *  # noqa: E402,F401,F403

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.PBKDF2PasswordHasher"]
