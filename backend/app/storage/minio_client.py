"""MinIO/S3 client for clips, thumbnails, model weights, datasets."""
from __future__ import annotations
import logging
from datetime import timedelta
from io import BytesIO

from minio import Minio
from minio.error import S3Error

from app.config import settings

log = logging.getLogger(__name__)


def _client() -> Minio:
    endpoint = settings.s3_endpoint.replace("http://", "").replace("https://", "")
    return Minio(
        endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        secure=settings.s3_use_ssl,
        region=settings.s3_region,
    )


def ensure_buckets() -> None:
    """Create the configured buckets if they don't exist (idempotent)."""
    c = _client()
    for b in (settings.s3_bucket_clips, settings.s3_bucket_thumbs,
              settings.s3_bucket_models, settings.s3_bucket_datasets):
        try:
            if not c.bucket_exists(b):
                c.make_bucket(b)
        except S3Error as e:
            log.warning("bucket %s: %s", b, e)


def put_bytes(bucket: str, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    c = _client()
    c.put_object(bucket, key, BytesIO(data), length=len(data), content_type=content_type)
    return f"{bucket}/{key}"


def get_bytes(bucket: str, key: str) -> bytes:
    c = _client()
    resp = c.get_object(bucket, key)
    try:
        return resp.read()
    finally:
        resp.close()
        resp.release_conn()


def presigned_get(bucket: str, key: str, *, expires_seconds: int = 3600) -> str:
    return _client().presigned_get_object(bucket, key, expires=timedelta(seconds=expires_seconds))
