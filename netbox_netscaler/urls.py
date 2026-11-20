"""URL routing for NetBox NetScaler plugin."""

from django.urls import path

from .views import RefreshView, StaleVipsView

urlpatterns = [
    path("stale-vips/", StaleVipsView.as_view(), name="stale_vips"),
    path("refresh/", RefreshView.as_view(), name="refresh"),
]
