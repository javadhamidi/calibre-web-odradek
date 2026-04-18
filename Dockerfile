FROM python:3.12-slim

ARG BUILD_DATE
ARG VERSION
ARG KEPUBIFY_VERSION=4.0.4

LABEL build_version="Version:- ${VERSION} Build-date:- ${BUILD_DATE}"
LABEL org.opencontainers.image.title="calibre-web"
LABEL org.opencontainers.image.description="Web app for browsing, reading and downloading eBooks stored in a Calibre database"
LABEL org.opencontainers.image.url="https://github.com/janeczku/calibre-web"
LABEL org.opencontainers.image.source="https://github.com/janeczku/calibre-web"
LABEL org.opencontainers.image.licenses="GPL-3.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CALIBRE_DBPATH=/config

# Install system deps, Python packages, and kepubify in one layer so
# build-time tools (gcc, dev headers) are available for pip but not kept
# in the final image.
COPY requirements.txt /tmp/requirements.txt

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libmagic1 \
        imagemagick \
        ghostscript \
        libldap2 \
        libsasl2-2 \
        gcc \
        libldap2-dev \
        libsasl2-dev \
    && pip install --upgrade pip \
    && pip install -r /tmp/requirements.txt \
    # optional extras bundled by default (mirrors linuxserver's optional-requirements)
    && pip install \
        gevent \
        greenlet \
        jsonschema \
        rarfile \
        natsort \
    # fetch kepubify binary (Kobo ePub converter), matching linuxserver's approach
    && ARCH="$(dpkg --print-architecture)" \
    && case "${ARCH}" in \
        amd64)  KA="kepubify-linux-64bit" ;; \
        arm64)  KA="kepubify-linux-arm64" ;; \
        armhf)  KA="kepubify-linux-arm"   ;; \
        *)      echo "Unsupported arch: ${ARCH}" && exit 1 ;; \
    esac \
    && curl -fsSL "https://github.com/pgaskin/kepubify/releases/download/v${KEPUBIFY_VERSION}/${KA}" \
         -o /usr/local/bin/kepubify \
    && chmod +x /usr/local/bin/kepubify \
    # remove build-time deps
    && apt-get purge -y gcc libldap2-dev libsasl2-dev curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* /tmp/requirements.txt

WORKDIR /app

COPY . .

RUN mkdir -p /config \
    && addgroup --system --gid 1000 abc \
    && adduser  --system --uid 1000 --ingroup abc --no-create-home abc \
    && chown -R abc:abc /app /config

VOLUME /config

EXPOSE 8083

USER abc

ENTRYPOINT ["python", "cps.py"]
