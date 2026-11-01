FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HARKENN_DATA_DIR=/data \
    HF_HOME=/data/models/huggingface

WORKDIR /srv
# cairo draws the assistant's SVG screens (cairosvg); DejaVu gives SVG text a font. The font cache is built here:
# the server can't write one at run time, and would rescan the fonts for every screen.
RUN apt-get update && apt-get install -y --no-install-recommends libcairo2 fonts-dejavu-core fontconfig  && fc-cache -f && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# For the admin About page: support, licences and credits, the speaker firmware's included
COPY CREDITS.md SUPPORT.md LICENSE ./
ADD https://raw.githubusercontent.com/harkenn-project/speaker-firmware/main/CREDITS.md FIRMWARE-CREDITS.md
RUN chmod 644 FIRMWARE-CREDITS.md  # downloaded files come readable by root only
# Draw the built-in characters once at build time instead of on every fresh start
ENV HARKENN_PREBUILT_CHARACTERS=/srv/prebuilt-characters
RUN python -m app.starter /srv/prebuilt-characters
ENV HARKENN_WAKEWORDS_DIR=/srv/wakewords
RUN python -m app.wakewords /srv/wakewords
RUN useradd --system --uid 10001 --home-dir /srv harkenn \
 && mkdir -p /data && chown harkenn /data

USER 10001
EXPOSE 8000
VOLUME /data
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
