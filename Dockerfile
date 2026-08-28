# The app, on the architecture it actually ships to.
#
# A Raspberry Pi 2 (BCM2836, Cortex-A7) is 32-bit ARMv7 — a genuinely different
# platform from arm64, not a slower version of it. Wheels come from piwheels
# rather than PyPI's aarch64 builds, and anything with a native extension
# (pydantic-core's Rust core, websockets) either has an armv7 wheel or compiles
# on a 900MHz core. An arm64 image cannot surface any of that, which is why the
# previous Dockerfile's `--platform linux/arm64` was testing a fiction.
#
# Build and run (see docker-compose.yml for the wired-up version):
#   docker buildx build --platform linux/arm/v7 -t music-manager:pi --load .
#   docker run --rm --platform linux/arm/v7 --memory 1g -p 8001:8000 music-manager:pi
#
# Everything runs under QEMU user-mode emulation, so it is slow — comparable to
# the real hardware, sometimes worse. That is the point: it makes the costs this
# app is designed around visible on a laptop.

FROM --platform=linux/arm/v7 python:3.11-slim-bookworm

# 3.11 is not incidental: piwheels publishes cp311 wheels for Bookworm, which is
# what Raspberry Pi OS ships. A different minor version means no wheel and a
# source build.

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg               — yt-dlp's audio extraction, and Shazam's decoding
# libchromaprint-tools — provides fpcalc, which AcoustID fingerprinting needs
# libopenblas0         — numpy's ARM wheel links against it, and shazamio
#                        imports numpy. Without it every Shazam call fails with
#                        "libopenblas.so.0: cannot open shared object file",
#                        which the provider swallows as "no match" — so
#                        recognition silently does nothing and looks merely
#                        unlucky. The x86 numpy wheels bundle their own BLAS,
#                        which is why this only bites on ARM. The Pi needs it
#                        too: apt install libopenblas0
# curl                 — container healthcheck
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ffmpeg \
        libchromaprint-tools \
        libopenblas0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# piwheels serves prebuilt ARM wheels; without it every native package compiles
# from source under emulation, which turns a two-minute install into an hour.
RUN printf '[global]\nextra-index-url = https://www.piwheels.org/simple\n' \
    > /etc/pip.conf

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Paths inside the container. docker-compose mounts host directories onto these,
# so the container's layout mirrors the Pi's without hardcoding /media/pi.
ENV LIBRARY_ROOT=/media/library \
    SCAN_ROOTS=/media/music:/media/youtube_music \
    DOWNLOAD_STAGING=/media/staging \
    DATABASE_PATH=/data/db.sqlite3 \
    DJANGO_ALLOWED_HOSTS=* \
    DJANGO_DEBUG=0 \
    LOG_LEVEL=INFO

RUN mkdir -p /media/library /media/music /media/youtube_music /media/staging /data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/fragments/stats/ || exit 1

COPY deploy/docker-entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# Matches deploy/music_manager.service: one process, threads for concurrency.
# --workers 1 is required, not a preference — the job queue's wake event, the
# SSE revision counter and the per-track locks are all in-process.
CMD ["gunicorn", \
     "--workers", "1", \
     "--threads", "16", \
     "--worker-class", "gthread", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--bind", "0.0.0.0:8000", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "music_manager.wsgi:application"]
