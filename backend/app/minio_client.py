"""
MinIO 客户端封装 —— 内部操作客户端 + 预签名 URL 生成

私有桶策略：桶不再设置匿名只读，所有下载（浏览器 / DashScope / 后端自取）
一律走带签名的限时 URL：
  - presign_url          对外（浏览器/DashScope），经 nginx /files/ 代理到 MinIO
  - presign_internal_url 后端内网自取（文本/PDF/Excel 内容提取）
"""
from datetime import timedelta
from minio import Minio
from app.config import settings

client = Minio(
    settings.minio_endpoint,
    access_key=settings.minio_access_key,
    secret_key=settings.minio_secret_key,
    secure=False,
)

BUCKET = settings.minio_bucket

# 旧的匿名只读策略只需撤销一次（模块级标记，避免每次上传都调 MinIO 策略接口）
_policy_migration_done = False


# 对外签名客户端（按配置构建一次，进程内复用；签名是本地计算，无需每次新建）
_sign_client_instance: Minio | None = None


def _sign_client() -> Minio:
    """面向外网的签名客户端：签出的 URL 走 nginx /files/ 代理，Host 与签名一致"""
    global _sign_client_instance
    if _sign_client_instance is not None:
        return _sign_client_instance
    if settings.public_server_ip:
        endpoint = f"{settings.public_server_ip}:{settings.public_server_port or '443'}"
        secure = settings.public_server_ssl
    else:
        # 未配置公网地址（本地开发）：退回 MinIO 直连端点
        endpoint = settings.minio_endpoint
        secure = False
    _sign_client_instance = Minio(
        endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=secure,
    )
    return _sign_client_instance


def db_url_to_object(db_url: str) -> str:
    """数据库中的文件 URL（/files/{bucket}/{object}）→ MinIO object name"""
    part = db_url or ""
    for prefix in ("http://", "https://"):
        if part.startswith(prefix):
            part = part[len(prefix):]
            pos = part.find("/")
            if pos >= 0:
                part = part[pos:]
            break
    segs = part.split("/", 3)  # ['', 'files', bucket, object]
    if len(segs) >= 4:
        return segs[3]
    return part.lstrip("/")


def presign_url(object_name: str, expires: int = 7200) -> str:
    """
    生成对外预签名下载 URL。
    原始签出路径为 /{bucket}/{object}，改写为 /files/{bucket}/{object}
    以命中 nginx 的 /files/ 代理（该代理保留原始 Host，通过 SigV4 校验）。
    """
    raw = _sign_client().presigned_get_object(
        BUCKET, object_name, expiry=timedelta(seconds=expires)
    )
    marker = f"/{BUCKET}/"
    pos = raw.find(marker)
    if pos < 0:
        return raw
    return raw[:pos] + "/files" + raw[pos:]


def presign_internal_url(object_name: str, expires: int = 600) -> str:
    """后端内网下载用签名 URL（内网直连 MinIO，适配私有桶）"""
    return client.presigned_get_object(
        BUCKET, object_name, expiry=timedelta(seconds=expires)
    )


def presign_from_db_url(db_url: str, expires: int = 7200) -> str:
    """按数据库 URL 生成对外预签名下载 URL（各 API 出口统一使用）"""
    return presign_url(db_url_to_object(db_url), expires)


def ensure_bucket():
    global _policy_migration_done
    if not client.bucket_exists(BUCKET):
        client.make_bucket(BUCKET)
        _policy_migration_done = True  # 新建的桶无匿名策略
    elif not _policy_migration_done:
        # 迁移：撤销历史的匿名只读策略（私有化后统一走预签名 URL）
        try:
            client.delete_bucket_policy(BUCKET)
        except Exception:
            pass  # 本就无策略时忽略
        _policy_migration_done = True
