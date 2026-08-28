"""
Root URL configuration.

The previous project included the same urlconf twice — once at `''` and once
under `'api/'` — so every route existed at two paths and `reverse()` silently
resolved to the `api/`-prefixed copy. Included exactly once here.
"""

from django.urls import include, path

urlpatterns = [
    path("", include("music.urls")),
]
