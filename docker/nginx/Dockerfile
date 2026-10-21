# ── Stage 1: 构建前端（CRA 生产构建）──
# 构建上下文为项目根目录（见 docker-compose.yaml 中 nginx 服务的 build.context）
FROM node:20-alpine AS front-build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ── Stage 2: Nginx + 前端产物 ──
FROM nginx:1.26-alpine

# 前端静态资源落到 nginx.conf 的 root 路径 /app/front
COPY --from=front-build /app/build /app/front

RUN apk add --no-cache openssl && \
    mkdir -p /etc/nginx/ssl /var/www/certbot && \
    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
      -keyout /etc/nginx/ssl/server.key \
      -out /etc/nginx/ssl/server.crt \
      -subj "/CN=localhost"
