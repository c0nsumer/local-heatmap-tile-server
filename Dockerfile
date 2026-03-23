FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libgeos-dev \
    nginx \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Copy nginx config and custom error page
COPY nginx.conf /etc/nginx/nginx.conf
COPY 502.html /usr/share/nginx/html/502.html
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Create data directories
RUN mkdir -p /data/import /data/db /data/tiles

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
