"""S3-compatible cold-tier storage client (MinIO / AWS S3) for PC21.

Cold tier holds Parquet objects written by the out-of-process archive cron job
(``scripts/cron/cleanup_audit_events.py``) and, from PC23, the trace aggregates.

The ``boto3`` and ``pyarrow`` imports are deliberately lazy (same pattern as
``agent_obs/eval/llm_judge_worker.py``) so that importing this module never pulls
in the archive toolchain — the SDK itself does not write to the cold tier.
"""

from __future__ import annotations

import io
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_REGION = "us-east-1"
PARQUET_COMPRESSION = "snappy"


class ColdStore:
    """Client for the S3-compatible cold tier.

    Connection is lazily established on first call so that constructing a
    ``ColdStore`` never requires ``boto3`` to be installed or the object store
    to be reachable.

    Parameters
    ----------
    endpoint:
        S3 endpoint URL (e.g. ``"http://minio:9000"``). Defaults to
        ``S3_ENDPOINT``.
    access_key / secret_key:
        Credentials. Default to ``S3_ACCESS_KEY`` / ``S3_SECRET_KEY``.
    region:
        Region name. MinIO ignores it; defaulted to ``us-east-1``.
    bucket:
        Default bucket. Defaults to ``S3_AUDIT_BUCKET``.
    verify_tls:
        Whether to verify TLS certificates when talking to the endpoint.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str = DEFAULT_REGION,
        bucket: str | None = None,
        verify_tls: bool = True,
    ) -> None:
        self._endpoint = endpoint or os.environ.get("S3_ENDPOINT", "http://localhost:9000")
        self._access_key = access_key or os.environ.get("S3_ACCESS_KEY", "")
        self._secret_key = secret_key or os.environ.get("S3_SECRET_KEY", "")
        self._region = region
        self._bucket = bucket or os.environ.get("S3_AUDIT_BUCKET", "audit-events")
        self._verify_tls = verify_tls
        self._client: Any | None = None

    @property
    def bucket(self) -> str:
        """Default cold-tier bucket name."""
        return self._bucket

    def _get_client(self) -> Any:
        """Lazy initialization of the boto3 S3 client."""
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError:
                raise RuntimeError(
                    "boto3 is required for ColdStore. "
                    "Install with: pip install 'agent-obs[cron]'"
                )

            self._client = boto3.client(
                "s3",
                endpoint_url=self._endpoint,
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
                region_name=self._region,
                verify=self._verify_tls,
                config=Config(signature_version="s3v4"),
            )
            logger.info(
                "S3 cold storage client created: endpoint=%s bucket=%s",
                self._endpoint,
                self._bucket,
            )
        return self._client

    def is_available(self) -> bool:
        """Check if the object store is reachable.

        Returns ``False`` instead of raising so that cron jobs can degrade
        gracefully (a failed archive must never lose the source rows).
        """
        try:
            self._get_client().list_buckets()
            return True
        except Exception as e:
            logger.debug("cold store health check failed: %s", str(e))
            return False

    def write_parquet(self, key: str, table: Any, bucket: str | None = None) -> int:
        """Serialise a ``pyarrow.Table`` to Snappy Parquet and upload it.

        The key must be deterministic for a given (partition, run window) so a
        re-run of the same batch overwrites the object instead of creating a
        duplicate — this is what makes the archive job idempotent.

        Parameters
        ----------
        key:
            Object key, e.g. ``"year=2026/month=09/day=28/part-1758326400.parquet"``.
        table:
            A ``pyarrow.Table``.
        bucket:
            Override the default bucket.

        Returns
        -------
        int
            Number of bytes uploaded.
        """
        import pyarrow.parquet as pq

        buffer = io.BytesIO()
        pq.write_table(table, buffer, compression=PARQUET_COMPRESSION)
        payload = buffer.getvalue()
        return self.write_bytes(key, payload, bucket=bucket)

    def write_bytes(self, key: str, payload: bytes, bucket: str | None = None) -> int:
        """Upload raw bytes to the cold tier.

        Returns
        -------
        int
            Number of bytes uploaded.
        """
        target = bucket or self._bucket
        self._get_client().put_object(Bucket=target, Key=key, Body=payload)
        logger.info("cold write bucket=%s key=%s bytes=%d", target, key, len(payload))
        return len(payload)

    def read_bytes(self, key: str, bucket: str | None = None) -> bytes:
        """Download raw bytes from the cold tier."""
        target = bucket or self._bucket
        response = self._get_client().get_object(Bucket=target, Key=key)
        return response["Body"].read()

    def list_keys(self, prefix: str = "", bucket: str | None = None) -> list[str]:
        """List object keys under ``prefix``."""
        target = bucket or self._bucket
        client = self._get_client()
        keys: list[str] = []
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=target, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        return keys

    def delete_prefix(self, prefix: str, bucket: str | None = None) -> int:
        """Delete all objects under ``prefix``.

        Returns
        -------
        int
            Number of objects deleted.
        """
        target = bucket or self._bucket
        client = self._get_client()
        
        # List all objects under the prefix
        paginator = client.get_paginator("list_objects_v2")
        objects_to_delete = []
        for page in paginator.paginate(Bucket=target, Prefix=prefix):
            for obj in page.get("Contents", []):
                objects_to_delete.append({"Key": obj["Key"]})
        
        if not objects_to_delete:
            return 0
        
        # Delete in batches (S3 limit is 1000 objects per request)
        total_deleted = 0
        batch_size = 1000
        for i in range(0, len(objects_to_delete), batch_size):
            batch = objects_to_delete[i:i + batch_size]
            response = client.delete_objects(
                Bucket=target,
                Delete={"Objects": batch}
            )
            total_deleted += len(response.get("Deleted", []))
            if response.get("Errors"):
                logger.warning("cold delete_prefix: some objects failed to delete: %s", response["Errors"])
        
        logger.info("cold delete_prefix: deleted %d objects from prefix %s", total_deleted, prefix)
        return total_deleted

    def close(self) -> None:
        """Drop the cached client. botocore pools are closed by the GC."""
        self._client = None

    def __enter__(self) -> "ColdStore":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


def parq_key(prefix: str, day: str, run_timestamp: int) -> str:
    """Build a deterministic Hive-style partition key for an archive batch.

    Parameters
    ----------
    prefix:
        Leading key segment, e.g. ``"year=2026/month=09/day=28"``. A leading
        slash is tolerated.
    day:
        Partition day in ``YYYYMMDD`` form (kept for logging).
    run_timestamp:
        Unix timestamp of the cron run; makes the file name unique per batch
        while staying deterministic for an idempotent re-run of that batch.

    Returns
    -------
    str
        e.g. ``"year=2026/month=09/day=28/part-1758326400.parquet"``.
    """
    normalized = prefix.strip("/")
    return f"{normalized}/part-{run_timestamp}.parquet"


__all__ = ["ColdStore", "PARQUET_COMPRESSION", "parq_key"]
