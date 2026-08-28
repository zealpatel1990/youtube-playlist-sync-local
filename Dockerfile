# Test image that matches the REAL deployment target.
#
# The previous Dockerfile built for linux/arm64. A Raspberry Pi 2 (BCM2836,
# Cortex-A7) is 32-bit ARMv7, which is a genuinely different platform: wheels
# come from piwheels rather than the aarch64 PyPI builds, and packages with
# native extensions (shazamio's dependencies especially) may have no armv7
# wheel at all and fall back to compiling — for hours, on a 900MHz core. An
# arm64 image cannot surface any of that.
#
# Build (needs binfmt/qemu registered once via tonistiigi/binfmt):
#   docker buildx build --platform linux/arm/v7 -t music-manager:pi --load .
#   docker run --rm --platform linux/arm/v7 music-manager:pi
#
# Emulated ARMv7 is slow. This image is for catching install and import
# failures, not for benchmarking.

FROM --platform=linux/arm/v7 python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg   — yt-dlp's audio extraction
# libchromaprint-tools — provides fpcalc, which AcoustID identification needs
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ffmpeg \
        libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# piwheels serves prebuilt ARM wheels; without it every native package compiles.
RUN printf '[global]\nextra-index-url = https://www.piwheels.org/simple\n' \
    > /etc/pip.conf

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Dummy values so settings import succeeds; the suite never uses them.
ENV DJANGO_SECRET_KEY=test-only-not-a-real-secret \
    DJANGO_DEBUG=0 \
    LIBRARY_ROOT=/tmp/library \
    SCAN_ROOTS=/tmp/scan \
    DOWNLOAD_STAGING=/tmp/staging \
    DATABASE_PATH=/tmp/test.sqlite3 \
    MUSIC_MANAGER_DISABLE_WORKERS=1

# Scoped to the app: a bare `manage.py test` would discover stray test*.py
# files at the repo root (docs/CODE-AUDIT.md A10).
CMD ["python", "manage.py", "test", "music", "-v", "2"]
