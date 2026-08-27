"""Root URLconf.

`studies.urls` is included here starting Phase 4/5, once the app exists.
Auth views (login/logout) are wired now since every project view will sit
behind `@login_required` from the start.
"""
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
]
