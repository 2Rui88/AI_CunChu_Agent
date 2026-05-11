from minio import Minio
from app.config import settings

client = Minio(
    settings.minio_endpoint,
    access_key=settings.minio_access_key,
    secret_key=settings.minio_secret_key,
    secure=False,
)

BUCKET = settings.minio_bucket


def ensure_bucket():
    import json
    if not client.bucket_exists(BUCKET):
        client.make_bucket(BUCKET)
        # 设置公开读权限，允许直接下载文件
        policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"AWS": ["*"]},
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{BUCKET}/*"],
            }],
        }
        client.set_bucket_policy(BUCKET, json.dumps(policy))
