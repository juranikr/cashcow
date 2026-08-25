FROM python:3.12-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579

# The service does not execute Perl. Removing the otherwise-essential Debian
# interpreter keeps newly disclosed Perl CVEs out of the runtime boundary.
RUN apt-get purge -y --allow-remove-essential perl-base \
  && rm -rf /var/lib/apt/lists/*

ARG RUNTIME_BUILD_SHA=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    RUNTIME_BUILD_SHA=${RUNTIME_BUILD_SHA}

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py engine.py storage.py world.py browser_client.py browser_planner.py cognition.py ./
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin cashcow \
  && chown -R cashcow:root /app
USER cashcow

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/cashcow/health')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
