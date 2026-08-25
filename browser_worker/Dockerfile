FROM mcr.microsoft.com/playwright/python:v1.62.0-noble@sha256:aa81288e738725378becba5b3e06cb0f3a7f012a610e87e8d767a090ea3f740d

# Keep the vendor image patched and remove media/network tooling that this
# read-only worker never invokes. Chromium ships its own media library.
RUN apt-get update \
  && apt-get upgrade -y \
  && apt-get purge -y \
    curl \
    libcurl4t64 \
    libcurl3t64-gnutls \
    libavcodec60 \
    libavfilter9 \
    libavformat60 \
    libavutil58 \
    libpostproc57 \
    libswresample4 \
    libswscale7 \
    libcjson1 \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

ARG BROWSER_WORKER_BUILD_SHA=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8001 \
    HOME=/home/pwuser \
    BROWSER_WORKER_BUILD_SHA=${BROWSER_WORKER_BUILD_SHA}

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py policy.py egress.py ./
RUN chown -R pwuser:pwuser /app /home/pwuser
USER pwuser

EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8001/health')); assert d['status']=='ok' and d['buildSha']!='unknown' and d.get('credentialIsolation') and not d.get('forbiddenCredentialEnvironmentPresent') and not d.get('awsCredentialsPresent') and not d.get('groqKeyPresent') and d.get('networkProxy') and d.get('pinnedHttpsProxy') and d.get('safeMethodsOnly') and d.get('chromiumSandboxRequired') and d.get('publicHttpsOnly')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]
