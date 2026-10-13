FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BRIDGE_CONFIG=/config/config.json
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge ./bridge
USER 10001:10001
EXPOSE 9080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9080/healthz', timeout=3)"
CMD ["uvicorn", "bridge.web:app", "--host", "0.0.0.0", "--port", "9080", "--workers", "1", "--no-access-log", "--timeout-graceful-shutdown", "150"]
