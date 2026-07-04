FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/srv/digitalcore

WORKDIR /srv/digitalcore

# System deps: postgres client libs are provided by asyncpg wheels; curl is only
# for optional debugging. Keep the image slim.
RUN apt-get update \
    && apt-get install -y --no-install-recommends dumb-init \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN chmod +x scripts/entrypoint.sh \
    && addgroup --system app && adduser --system --ingroup app app \
    && chown -R app:app /srv/digitalcore
USER app

ENTRYPOINT ["dumb-init", "--", "scripts/entrypoint.sh"]
CMD ["backend"]
