# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.11

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip build && \
    pip wheel --wheel-dir /wheels .

FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    YOINK_WORKDIR=/tmp/yoink \
    YOINK_CACHE_DB=/data/yoink.sqlite

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        tini && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 1000 yoink && \
    useradd --system --uid 1000 --gid yoink --create-home --home-dir /home/yoink yoink && \
    mkdir -p /app /data /tmp/yoink && \
    chown -R yoink:yoink /app /data /tmp/yoink

COPY --from=builder /wheels /wheels

# gallery-dl >=1.28 is required for any TikTok support; the native
# TiktokPostExtractor (photo/video) and TiktokVmpostExtractor (vt./vm. short
# links) are what make photo-slideshow posts downloadable. yt-dlp still cannot
# extract TikTok /photo/ posts at any version, so gallery-dl is the only path
# for slideshows (the provider already falls back to it).
ARG GALLERY_DL_VERSION=1.32.2
ARG YT_DLP_VERSION=2026.3.17

RUN pip install --no-index --find-links=/wheels yoink && \
    pip install "gallery-dl==${GALLERY_DL_VERSION}" "yt-dlp==${YT_DLP_VERSION}" && \
    rm -rf /wheels

WORKDIR /app
USER yoink

ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "yoink"]
