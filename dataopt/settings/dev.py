"""Local development overrides. Never used in production."""
from .base import *  # noqa: F401,F403
from .base import env, env_bool

DEBUG = env_bool("DJANGO_DEBUG", True)

if not SECRET_KEY:
    # Fine for local development only — prod.py refuses to start without a
    # real DJANGO_SECRET_KEY set in the environment.
    SECRET_KEY = "dev-insecure-secret-key-do-not-use-in-production"  # noqa: S105

if not ALLOWED_HOSTS:
    ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
