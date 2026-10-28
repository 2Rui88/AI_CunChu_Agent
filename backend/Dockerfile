FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    libopenblas-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# 直连 files.pythonhosted.org 实测仅十几 kB/s 且易超时，换阿里云 PyPI 镜像
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

COPY . .
RUN mkdir -p /data/faiss/users /data/chunks /tmp/faiss_locks

EXPOSE 8000

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app.main:app"]
