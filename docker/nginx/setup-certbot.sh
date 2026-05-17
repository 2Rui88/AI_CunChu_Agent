#!/bin/bash
# Let's Encrypt 证书申请与自动续期
#
# 前置条件：
#   1. 服务器有公网 IP，域名 DNS 已指向该 IP
#   2. 80 端口可从公网访问
#   3. 替换 YOUR_DOMAIN 为实际域名
#
# 用法：
#   chmod +x setup-certbot.sh
#   sudo ./setup-certbot.sh your-domain.com

set -e

DOMAIN="${1:?请提供域名，例如: ./setup-certbot.sh yuncunchu.com}"

echo "=== 为 ${DOMAIN} 申请 Let's Encrypt 证书 ==="

# 安装 certbot
if ! command -v certbot &> /dev/null; then
    apt-get update && apt-get install -y certbot
fi

# 创建 webroot 目录
mkdir -p /var/www/certbot

# 申请证书（webroot 模式，Nginx 负责 serve 验证文件）
certbot certonly --webroot \
    -w /var/www/certbot \
    -d "${DOMAIN}" \
    --email "admin@${DOMAIN}" \
    --agree-tos \
    --non-interactive

# 复制证书到 Nginx SSL 目录
CERT_DIR="/etc/letsencrypt/live/${DOMAIN}"
NGINX_SSL_DIR="$(dirname "$0")/ssl"

cp "${CERT_DIR}/fullchain.pem" "${NGINX_SSL_DIR}/server.crt"
cp "${CERT_DIR}/privkey.pem"   "${NGINX_SSL_DIR}/server.key"
chmod 600 "${NGINX_SSL_DIR}/server.key"

# 重载 Nginx
docker exec tc_nginx nginx -s reload 2>/dev/null || true

echo "=== 证书申请完成 ==="
echo ""
echo "自动续期 cron（每月执行）："
echo "0 3 1 * * certbot renew --quiet && cp ${CERT_DIR}/fullchain.pem ${NGINX_SSL_DIR}/server.crt && cp ${CERT_DIR}/privkey.pem ${NGINX_SSL_DIR}/server.key && docker exec tc_nginx nginx -s reload"
