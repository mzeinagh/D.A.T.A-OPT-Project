from django.urls import path

from . import views

app_name = "studies"

urlpatterns = [
    path("upload/", views.upload_view, name="upload"),
    path("upload/<int:batch_id>/", views.upload_status_view, name="upload_status"),
    path("upload/<int:batch_id>/review/", views.review_view, name="review"),
]
