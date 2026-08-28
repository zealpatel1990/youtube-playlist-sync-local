#!/bin/sh
# Prepare the container, then hand off to whatever was asked for.
#
# `exec "$@"` at the end matters: gunicorn becomes PID 1 and receives SIGTERM
# directly, so `docker stop` shuts the workers down gracefully instead of the
# shell swallowing the signal and the container being killed after the timeout.
set -e

echo "music-manager entrypoint: $(uname -m), python $(python -V 2>&1 | cut -d' ' -f2)"

# A dev key so the image runs out of the box. Anything real must pass its own.
if [ -z "$DJANGO_SECRET_KEY" ]; then
    export DJANGO_SECRET_KEY="container-dev-key-not-for-any-real-deployment"
    echo "  DJANGO_SECRET_KEY not set; using the throwaway container default"
fi

# The mounts may arrive owned by the host user with no write bit for us.
for dir in "$LIBRARY_ROOT" "$DOWNLOAD_STAGING" "$(dirname "$DATABASE_PATH")"; do
    [ -n "$dir" ] || continue
    mkdir -p "$dir" 2>/dev/null || true
    if [ ! -w "$dir" ]; then
        echo "  WARNING: $dir is not writable; the app will fail when it tries to use it"
    fi
done

python manage.py migrate --noinput
python manage.py collectstatic --noinput --verbosity 0

# Report what identification will actually be able to do, rather than letting
# the operator discover it from a wall of failed jobs.
echo "  ffmpeg: $(command -v ffmpeg >/dev/null && ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f1-3 || echo MISSING)"
echo "  fpcalc: $(command -v fpcalc >/dev/null && echo present || echo MISSING)"
[ -n "$ACOUSTID_API_KEY" ] || echo "  ACOUSTID_API_KEY unset -> the acoustid provider will be skipped"
[ -n "$GEMINI_API_KEY" ]   || echo "  GEMINI_API_KEY unset -> the gemini provider will be skipped"

exec "$@"
