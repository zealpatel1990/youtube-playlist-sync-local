from __future__ import annotations

import logging

from django.apps import AppConfig
from django.conf import settings

log = logging.getLogger("music")


class MusicConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "music"
    verbose_name = "Music library"

    def ready(self) -> None:
        # Register job handlers here, NOT in the worker bootstrap: manage.py
        # disables the worker pool, and registering there would leave the
        # registry empty under runserver, so every view that enqueues a job
        # would raise "unknown job kind". Enqueuing needs the registry; only
        # the pool needs threads.
        from music.jobs import registry

        registry.load_handlers()

        # Legal but risky configuration. Warnings rather than errors: refusing
        # to boot a live install would be worse than the risk.
        if settings.DEBUG:
            log.warning(
                "DJANGO_DEBUG is on: verbose error pages are served to anyone who "
                "can reach this port. Set DJANGO_DEBUG=0 for the Pi."
            )
        if "*" in settings.ALLOWED_HOSTS:
            log.warning(
                "DJANGO_ALLOWED_HOSTS accepts any Host header. Pin it to the Pi's "
                "hostname or IP."
            )
        if not settings.ACOUSTID_API_KEY and "acoustid" in settings.IDENTIFY_CHAIN:
            log.warning(
                "acoustid is in IDENTIFY_CHAIN but ACOUSTID_API_KEY is unset; that "
                "provider will be skipped. Get a free key at https://acoustid.org/."
            )
