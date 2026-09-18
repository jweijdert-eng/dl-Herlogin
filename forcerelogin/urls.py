"""App URLs"""

from django.urls import path

from . import views

app_name: str = "forcerelogin"

urlpatterns = [
    path("", views.index, name="index"),
    path("force/<int:user_id>/", views.force_user, name="force"),
    path("cancel/<int:user_id>/", views.cancel_user, name="cancel"),
    path("bulk/", views.force_bulk, name="bulk"),
    path("alts/", views.alts, name="alts"),
    path("alts/login/", views.alt_login, name="alt_login"),
]
