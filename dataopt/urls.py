"""Root URLconf.

Auth views (login/logout) are wired since every project view sits behind
`@login_required` from the start (decision 4). `studies.urls` lands here
as of Phase 4's upload flow.
"""
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("studies/", include("studies.urls")),
]

if settings.DEBUG:
    # Local/dev only — a real deployment serves MEDIA_URL from
    # object storage or a dedicated file server, never through Django itself.
    from django.conf.urls.static import static

    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
