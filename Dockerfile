FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/srv/digitalcore

WORKDIR /srv/digitalcore

# System deps: postgres client libs are provided by asyncpg wheels; curl is only
# for optional debugging. `gosu` lets the entrypoint drop root -> app after it has
# fixed ownership of the bind-mounted storage volume. Keep the image slim.
RUN apt-get update \
    && apt-get install -y --no-install-recommends dumb-init gosu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN chmod +x scripts/entrypoint.sh \
    && addgroup --system app && adduser --system --ingroup app app \
    && chown -R app:app /srv/digitalcore
# No `USER app` here on purpose: the container starts as root so the entrypoint
# can chown the bind-mounted storage volume (owned by the host user at runtime),
# then it drops to the unprivileged `app` user via gosu before exec-ing the
# service. This is the fix for receipt writes failing under the non-root user.

ENTRYPOINT ["dumb-init", "--", "scripts/entrypoint.sh"]
CMD ["backend"]
