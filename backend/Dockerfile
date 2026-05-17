FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    libopenblas-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /data/faiss/users /tmp/faiss_locks

EXPOSE 8000

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app.main:app"]
