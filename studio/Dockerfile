# MarkeVita AI Series Studio — backend service image.
#
# Build from the REPOSITORY ROOT, not from studio/, because the image needs
# both the studio and the v0.3 engine it drives:
#
#   docker build -f studio/Dockerfile -t markevita-studio .
#
# ffmpeg is a hard requirement: the assemble and QA stages shell out to it.

FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pipeline/requirements.txt /app/pipeline/requirements.txt
COPY studio/requirements.txt   /app/studio/requirements.txt
RUN pip install --no-cache-dir -r /app/pipeline/requirements.txt \
                               -r /app/studio/requirements.txt

COPY pipeline/ /app/pipeline/
COPY studio/   /app/studio/

WORKDIR /app/studio

# Served behind a proxy at markevita.com/studio.
ENV STUDIO_BASE_PATH=/studio \
    STUDIO_HOST=0.0.0.0 \
    STUDIO_PORT=8800 \
    STUDIO_ALLOW_PAID=false \
    PYTHONUNBUFFERED=1

EXPOSE 8800

# One worker on purpose: production jobs run as in-process background threads,
# so several workers would each hold their own job table in memory. Scaling out
# means moving the queue into Supabase first.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8800", "--workers", "1"]
