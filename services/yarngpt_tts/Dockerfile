# YarnGPT speech service. Build from the repository root:
#   docker build -f services/yarngpt_tts/Dockerfile -t yarngpt-tts .
# CPU-only image (the model has 0.4B parameters). For a GPU host, swap the torch line for the
# CUDA wheels; nothing else changes.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl libsndfile1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
ENV PYTHONPATH=/srv PYTHONUNBUFFERED=1 HF_HOME=/models/hf

RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu
COPY services/yarngpt_tts/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# YarnGPT's tokenizer code and speaker files, pinned to a known commit (MIT licence).
ARG YARNGPT_REF=8bb0eb27d307f4b5903149e24ecc1bd3caa4af2d
RUN mkdir -p /srv/yarngpt/default_speakers \
    && B=https://raw.githubusercontent.com/saheedniyi02/yarngpt/${YARNGPT_REF} \
    && for f in __init__.py audiotokenizer.py; do curl -fsSL $B/$f -o /srv/yarngpt/$f; done \
    && for n in azeez chinenye emma idera joke jude onye osagie regina remi saheed tayo umar zainab; do \
         curl -fsSL $B/default_speakers/$n.json -o /srv/yarngpt/default_speakers/$n.json; done

# Model weights, baked into the image so a restart never waits on the internet.
RUN mkdir -p /models \
    && curl -fsSL https://huggingface.co/novateur/WavTokenizer-medium-speech-75token/resolve/main/wavtokenizer_mediumdata_frame75_3s_nq1_code4096_dim512_kmeans200_attn.yaml -o /models/wavtokenizer.yaml \
    && gdown 1-ASeEkrn4HY49yZWHTASgfGFNXdVnLTt -O /models/wavtokenizer.ckpt \
    && python -c "from huggingface_hub import snapshot_download; snapshot_download('saheedniyi/YarnGPT2')"

COPY services/__init__.py /srv/services/__init__.py
COPY services/yarngpt_tts /srv/services/yarngpt_tts

EXPOSE 8080
CMD ["uvicorn", "services.yarngpt_tts.app:app", "--host", "0.0.0.0", "--port", "8080"]
