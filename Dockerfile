FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY policy.yaml ./policy.yaml
COPY src ./src
COPY static ./static

EXPOSE 8080
CMD ["uvicorn", "src.gateway.main:app", "--host", "0.0.0.0", "--port", "8080"]
