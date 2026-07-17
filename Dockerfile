FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY . .
RUN mkdir -p /app/data/toronto_browser_profile /app/data/toronto_debug

CMD ["python", "-m", "dev_project_monitor.monitor", "--config", "config.yml"]
