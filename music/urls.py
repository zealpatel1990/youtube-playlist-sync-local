"""
URL map for the whole app.

Mounted at exactly one prefix. The previous project included its urlconf under
both `''` and `'api/'`, which meant every name resolved twice and `reverse()`
bound to whichever include ran last — so `{% url %}` in the dashboard silently
produced `/api/...` links. One include, one set of names.
"""

from django.urls import path

from . import views

urlpatterns = [
    # --- pages ---------------------------------------------------------
    path("", views.dashboard, name="dashboard"),
    path("review/", views.library_review, name="library_review"),
    path("duplicates/", views.duplicates, name="duplicates"),
    path("jobs/", views.jobs, name="jobs"),
    path("jobs/<int:pk>/", views.job_detail, name="job_detail"),

    # --- live updates --------------------------------------------------
    path("events/", views.stream_events, name="stream_events"),
    path("fragments/tracks/", views.fragment_tracks, name="fragment_tracks"),
    path("fragments/stats/", views.fragment_stats, name="fragment_stats"),
    path("fragments/jobs/", views.fragment_jobs, name="fragment_jobs"),

    # --- settings (.env editor) ----------------------------------------
    path("settings/", views.settings_form, name="settings_form"),
    path("settings/save/", views.update_settings, name="update_settings"),

    # --- actions (POST only; each enqueues a job and returns 204) -------
    path("actions/scan/", views.action_scan, name="action_scan"),
    path("actions/plan/", views.action_plan, name="action_plan"),
    path("actions/apply/", views.action_apply, name="action_apply"),
    path("actions/sync-youtube/", views.action_sync_youtube, name="action_sync_youtube"),
    path("actions/update-ytdlp/", views.action_update_ytdlp, name="action_update_ytdlp"),
    path(
        "actions/track/<int:pk>/suggest/",
        views.action_suggest_track,
        name="action_suggest_track",
    ),
    path(
        "fragments/track/<int:pk>/suggestions/",
        views.fragment_suggestions,
        name="fragment_suggestions",
    ),
    path(
        "actions/track/<int:pk>/suggestion/<int:index>/use/",
        views.action_apply_suggestion,
        name="action_apply_suggestion",
    ),
    path(
        "actions/track/<int:pk>/identify/",
        views.action_identify_track,
        name="action_identify_track",
    ),
    path(
        "actions/track/<int:pk>/organize/",
        views.action_organize_track,
        name="action_organize_track",
    ),
    path(
        "actions/track/<int:pk>/revert/",
        views.action_revert_track,
        name="action_revert_track",
    ),
    path(
        "actions/video/<str:pk>/download/",
        views.action_download_video,
        name="action_download_video",
    ),
]
