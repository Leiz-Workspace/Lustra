# Lustra gender model server (python -m lustra.model_server, docs/operations.md §3c)
#   docker build -f tools/model_server/Dockerfile -t lustra-model-server .
#   docker run -d --name lustra-model -e LUSTRA_GENDER_MODEL_TOKEN=<token> -p 127.0.0.1:8765:8765 \
#     -v lustra-model:/models lustra-model-server
# The model (~380 MB, pinned revision, sha256 checked) is downloaded into the /models volume on the first start.
# Listening on 0.0.0.0 inside the container needs a token (the server refuses to start without one).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LUSTRA_GENDER_MODEL_DIR=/models \
    LUSTRA_DATA_DIR=/data

# ffmpeg: frames of videos for POST /v1/frames (-gender_verify_media); animated pictures only need Pillow
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# the server needs only these: the downloader dependencies of requirements.txt are not used
RUN pip install --no-cache-dir 'requests>=2.31' 'numpy>=1.26' 'onnxruntime>=1.18' 'pillow>=10'

WORKDIR /srv
COPY lustra ./lustra
COPY LICENSE THIRD_PARTY_NOTICES.md ./
RUN useradd --system --uid 10001 --home-dir /data lustra \
    && mkdir -p /models /data \
    && chown lustra /models /data
USER lustra

VOLUME /models
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s --start-period=600s \
    CMD python -c "import os, urllib.request as u; r = u.Request('http://127.0.0.1:8765/v1/status', headers={'Authorization': 'Bearer ' + os.environ.get('LUSTRA_GENDER_MODEL_TOKEN', '')}); u.urlopen(r, timeout=4)"
CMD ["python", "-m", "lustra.model_server", "--host", "0.0.0.0", "--port", "8765"]
