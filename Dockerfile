FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KOTODAMA_DATA_DIR=/data \
    HF_HOME=/data/models/huggingface

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# Draw the built-in characters once at build time instead of on every fresh start
ENV KOTODAMA_PREBUILT_CHARACTERS=/srv/prebuilt-characters
RUN python -m app.starter /srv/prebuilt-characters
RUN useradd --system --uid 10001 --home-dir /srv kotodama \
 && mkdir -p /data && chown kotodama /data

USER 10001
EXPOSE 8000
VOLUME /data
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
