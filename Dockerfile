FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

EXPOSE 5000

ENV PYTHONUNBUFFERED=1

# Mount /app/data as a volume to persist scan results between container restarts.
# Credentials are entered via the web UI per scan and are never written to disk.
VOLUME /app/data

CMD ["python", "app.py", "--host", "0.0.0.0", "--port", "5000"]
