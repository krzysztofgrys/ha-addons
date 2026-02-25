FROM python:3.9-slim

ENV LANG=C.UTF-8
ENV PYTHONUNBUFFERED=1
ENV SILERO_MODEL_PATH=/opt/silero/silero_vad.onnx

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg jq && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir onnxruntime numpy requests

RUN mkdir -p /opt/silero && \
    python -c "\
import urllib.request, os; \
url = 'https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx'; \
urllib.request.urlretrieve(url, os.environ['SILERO_MODEL_PATH']); \
size = os.path.getsize(os.environ['SILERO_MODEL_PATH']); \
print(f'Silero VAD ONNX model downloaded: {size:,} bytes')"

COPY run.sh /
COPY main.py /

RUN chmod +x /run.sh

CMD ["/run.sh"]
