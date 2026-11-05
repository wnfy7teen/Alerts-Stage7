FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 HOST=0.0.0.0 PORT=8000

# непривилегированный пользователь: при взломе приложения у процесса нет прав root
RUN useradd --system --uid 10001 --create-home --home-dir /home/app app \
    && mkdir -p /data /backups && chown app:app /data /backups

WORKDIR /app
COPY requirements.lock requirements.txt ./
RUN pip install -r requirements.lock

COPY server.py payments.py security.py ./
COPY static ./static
COPY scripts ./scripts

USER app
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','8000'), timeout=3)" || exit 1

CMD ["python", "server.py"]
