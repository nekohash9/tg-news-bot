FROM python:3.11-slim

WORKDIR /app
COPY . /app

RUN apt-get update && apt-get install -y build-essential gcc libxml2-dev libxslt-dev && \
    pip install --no-cache-dir -r requirements.txt && \
    apt-get remove -y build-essential gcc && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
CMD ["python", "main.py"]
