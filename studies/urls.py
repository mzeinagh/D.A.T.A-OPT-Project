from django.urls import path

from . import views

app_name = "studies"

urlpatterns = [
    path("upload/", views.upload_view, name="upload"),
    path("upload/<int:batch_id>/", views.upload_status_view, name="upload_status"),
    path("upload/<int:batch_id>/review/", views.review_view, name="review"),
    path("upload/<int:batch_id>/retry/", views.retry_detection_view, name="retry_detection"),
    path("upload/<int:batch_id>/process-as-single/", views.process_as_single_view, name="process_as_single"),
    path("", views.study_list_view, name="study_list"),
    path("<int:study_id>/", views.study_detail_view, name="study_detail"),
    path("<int:study_id>/runs/<int:run_id>/", views.run_detail_view, name="run_detail"),
    path("<int:study_id>/runs/<int:run_id>/export/full/", views.export_run_full_view, name="export_run_full"),
    path("<int:study_id>/runs/<int:run_id>/export/answers/", views.export_run_answers_view, name="export_run_answers"),
]
