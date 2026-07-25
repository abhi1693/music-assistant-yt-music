# Declare the build argument before FROM so it can be used in the base image tag
ARG MA_VERSION=latest
FROM ghcr.io/music-assistant/server:${MA_VERSION}

# Add OCI labels for basic image introspection
LABEL org.opencontainers.image.source="https://github.com/abhi1693/music-assistant-yt-music" \
      org.opencontainers.image.description="Music Assistant Server with pre-installed ytmusic provider"

# Copy the provider directory from the repository context into the image
COPY ytmusic/ /tmp/ytmusic/

# Detect the active Python version and move files to the correct site-packages folder.
RUN PYVER="" && \
    for d in /app/venv/lib/python3.*/; do \
        if [ -d "$d" ]; then \
            PYVER=$(basename "$d"); \
            break; \
        fi; \
    done && \
    if [ -z "$PYVER" ]; then PYVER="python3.13"; fi && \
    DST_DIR="/app/venv/lib/$PYVER/site-packages/music_assistant/providers/ytmusic" && \
    rm -rf "$DST_DIR" && \
    mv /tmp/ytmusic "$DST_DIR"
