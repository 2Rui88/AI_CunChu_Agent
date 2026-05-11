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
    if not client.bucket_exists(BUCKET):
        client.make_bucket(BUCKET)
