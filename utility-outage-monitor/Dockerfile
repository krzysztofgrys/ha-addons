FROM python:3.11-slim

ENV LANG=C.UTF-8
ENV PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends jq ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir requests flask apscheduler

COPY run.sh /
COPY main.py /
COPY app.py /

RUN chmod +x /run.sh

CMD ["/run.sh"]
