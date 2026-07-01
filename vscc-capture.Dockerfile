# VSCapture capture service, containerized (LAN mode).
# Bundles VSCaptureCLI by John George K (LGPL v3), downloaded from its official
# SourceForge release at build time — see Credits & License in README.md.

FROM mcr.microsoft.com/dotnet/runtime:8.0

LABEL org.opencontainers.image.source="https://github.com/chsbusch-dot/vscc-mqtt-server" \
      org.opencontainers.image.description="VSCapture-based Philips IntelliVue capture (research/education use only — not a medical device)" \
      org.opencontainers.image.licenses="MIT AND LGPL-3.0-only"

ARG VSCAPTURE_URL="https://sourceforge.net/projects/vscapture/files/VSCaptureCLI/VSCaptureCLIv1.007Binary.zip/download"

RUN apt-get update && apt-get install -y --no-install-recommends \
        iputils-ping procps bsdutils python3-minimal wget unzip \
    && rm -rf /var/lib/apt/lists/*

RUN wget -q -O /tmp/vscapture.zip "$VSCAPTURE_URL" \
    # Integrity guard: SourceForge can serve an HTML error page (small) instead
    # of the binary, and a truncated download would extract partially. Require a
    # plausible size and a valid archive before extracting.
    && [ "$(stat -c%s /tmp/vscapture.zip)" -gt 100000 ] \
       || { echo "VSCapture download failed or too small — aborting build"; exit 1; } \
    && unzip -tq /tmp/vscapture.zip \
    && mkdir -p /opt/vscapture \
    && unzip -q /tmp/vscapture.zip -d /opt/vscapture \
    && rm /tmp/vscapture.zip

COPY vscc-capture-entrypoint.sh /usr/local/bin/vscc-capture-entrypoint.sh
COPY vscc-file-cleanup.py /opt/vscc-file-cleanup.py
RUN chmod +x /usr/local/bin/vscc-capture-entrypoint.sh

# Export files live on a named volume shared with the worker and streamer
VOLUME /data
WORKDIR /data

ENTRYPOINT ["/usr/local/bin/vscc-capture-entrypoint.sh"]
