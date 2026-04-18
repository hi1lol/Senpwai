FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY senpwai/ senpwai/

# Default download path used by SETTINGS
RUN mkdir -p /root/Downloads/Anime

EXPOSE 8080

CMD ["python", "-m", "senpwai.webui.server", "--host", "0.0.0.0", "--port", "8080"]
