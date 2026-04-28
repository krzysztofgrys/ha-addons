FROM python:3.11-slim

ENV LANG=C.UTF-8
ENV PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg jq ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir flask

COPY run.sh /
COPY app.py /
COPY timelapse.py /
COPY storage.py /

RUN chmod +x /run.sh

CMD ["/run.sh"]
