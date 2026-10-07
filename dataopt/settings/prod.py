"""Production overrides. Fails fast on missing required secrets rather than
silently falling back to an insecure default."""
from .base import *  # noqa: F401,F403
from .base import MIDDLEWARE, env, env_bool

DEBUG = False

if not SECRET_KEY:
    raise RuntimeError("DJANGO_SECRET_KEY must be set in production.")

if not ALLOWED_HOSTS:
    raise RuntimeError("DJANGO_ALLOWED_HOSTS must be set in production.")

SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
SECURE_HSTS_INCLUDE_SUBDOMAINS = True

# Azure Container Apps ingress ends HTTPS and forwards plain HTTP with
# X-Forwarded-Proto: https. Without this, Django sees every request as HTTP
# and SECURE_SSL_REDIRECT redirects forever. Safe only because the container
# is reachable solely through that ingress, which always sets the header.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Only needed for a custom domain; the app's own HTTPS address is already
# trusted once the proxy header above is set. Comma-separated, with scheme.
CSRF_TRUSTED_ORIGINS = [o for o in env("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

# No Nginx in front of the container, so WhiteNoise serves static files
# (the admin's CSS/JS) from STATIC_ROOT, filled by collectstatic at build.
MIDDLEWARE = [
    MIDDLEWARE[0],  # SecurityMiddleware stays first
    "whitenoise.middleware.WhiteNoiseMiddleware",
    *MIDDLEWARE[1:],
]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
