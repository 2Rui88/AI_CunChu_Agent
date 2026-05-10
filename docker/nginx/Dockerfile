FROM nginx:1.26-alpine

RUN apk add --no-cache openssl && \
    mkdir -p /etc/nginx/ssl /app/front && \
    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
      -keyout /etc/nginx/ssl/server.key \
      -out /etc/nginx/ssl/server.crt \
      -subj "/CN=localhost"
