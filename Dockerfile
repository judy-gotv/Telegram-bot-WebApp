# Author: Passwd Word
FROM python:3.12-alpine
WORKDIR /app
COPY app/ /app/
ENV PORT=8080 DB_PATH=/data/anon.db PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/health >/dev/null 2>&1 || exit 1
CMD ["python", "main.py"]
