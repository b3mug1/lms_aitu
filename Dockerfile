FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install minimal runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# Copy application source
COPY app/ ./app/
COPY scripts/ ./scripts/

# Create data directory for persistent files
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["python", "-m", "app.main"]
