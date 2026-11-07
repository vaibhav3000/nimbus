"""Where results and the cloud's own logs live (PRD: S3 for maps, outputs and logs; OUTPUT_READY carries "where the
result is stored").

`ResultStore` reads small facts about a result the edge reports (size, type, a short text preview) and writes the
cloud's own objects (a finished task's archive, a lease bundle). Backed by a local folder, or by one S3 bucket when
`NIMBUS_RESULTS_BUCKET` is set. It never reads outside that bucket (a `result_ref` comes from the edge and is not
trusted to name any object in the account), never returns more than a short preview, and never raises: a result that
cannot be read just has no preview.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
PREVIEW_BYTES = 4000
TEXT_TYPES = ("text/", "application/json")


class ResultStore:
    def __init__(self, root: Path | None = None, bucket: str | None = None, client=None):
        self.root = Path(root) if root else None
        self.bucket, self._client = bucket, client

    @classmethod
    def from_env(cls, data_dir: Path) -> "ResultStore":
        bucket = os.environ.get("NIMBUS_RESULTS_BUCKET")
        client = None
        if bucket:
            import boto3
            client = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "ap-south-1"))
        return cls(root=Path(data_dir) / "results", bucket=bucket, client=client)

    # -- reading what the edge says it stored ------------------------------------------------------------------------------

    def describe(self, ref: str) -> dict:
        info = {"ref": ref, "readable": False}
        try:
            if ref.startswith("s3://") and self.bucket and self._client:
                bucket, _, key = ref[5:].partition("/")
                if bucket != self.bucket or not key or ".." in key:
                    return info                       # only our own bucket, only plain keys
                head = self._client.head_object(Bucket=bucket, Key=key)
                info.update(readable=True, size=head["ContentLength"], content_type=head.get("ContentType", ""))
                if info["content_type"].startswith(TEXT_TYPES) and info["size"] <= 10 * PREVIEW_BYTES:
                    body = self._client.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{PREVIEW_BYTES - 1}")["Body"].read()
                    info["preview"] = body.decode("utf-8", "replace")
            elif self.root and "://" in ref:
                rel = ref.split("://", 1)[1]
                path = (self.root / rel).resolve()
                if self.root.resolve() in path.parents and path.is_file():
                    data = path.read_bytes()
                    info.update(readable=True, size=len(data), content_type="text/plain" if path.suffix in (".txt", ".json", ".log") else "")
                    if info["content_type"]:
                        info["preview"] = data[:PREVIEW_BYTES].decode("utf-8", "replace")
        except Exception as exc:                      # an unreadable result must not stop the task from closing
            info["error"] = type(exc).__name__
        if ref.startswith("storage://") and not info["readable"]:
            # The edge's own placeholder for a file that is still on the edge. Say so, instead of looking like an
            # empty result: the cloud has the pointer, not the file, until the edge uploads it (see presign_put).
            info.update(uploaded=False, location="edge")
        return info

    # -- receiving what the edge uploads ---------------------------------------------------------------------------------

    def upload_key(self, task_id: str, name: str) -> str | None:
        """The object key an edge upload for this task and file name is stored under; None if the name is not plain."""
        if not isinstance(name, str) or not _NAME.fullmatch(name) or ".." in name:
            return None
        return f"results/{task_id}/{name}"

    def presign_put(self, task_id: str, name: str, content_type: str = "application/octet-stream",
                    expires_s: int = 900) -> dict | None:
        """Where the edge sends a result file: {"method": "PUT", "url", "ref", "headers"}. With a bucket it is a
        short-lived presigned S3 URL (the edge holds no AWS credentials); locally it is this server's own upload
        route. `ref` is what the edge puts in OUTPUT_READY once the PUT has succeeded."""
        key = self.upload_key(task_id, name)
        if key is None:
            return None
        if self.bucket and self._client:
            url = self._client.generate_presigned_url(
                "put_object", Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type}, ExpiresIn=expires_s)
            return {"method": "PUT", "url": url, "ref": f"s3://{self.bucket}/{key}", "headers": {"Content-Type": content_type},
                    "max_bytes": MAX_UPLOAD_BYTES}
        if self.root:
            return {"method": "PUT", "url": f"/{key.replace('results/', 'results/upload/', 1)}", "ref": f"file://{key}",
                    "headers": {}, "max_bytes": MAX_UPLOAD_BYTES}
        return None

    def save_upload(self, task_id: str, name: str, data: bytes) -> str | None:
        """Local-mode receiver for the upload route above. Returns the reference, or None if refused."""
        key = self.upload_key(task_id, name)
        if key is None or len(data) > MAX_UPLOAD_BYTES or self.bucket:
            return None
        return self.put(key, data, "application/octet-stream")

    # -- writing the cloud's own objects -----------------------------------------------------------------------------------

    def put(self, key: str, data: bytes, content_type: str = "application/json") -> str | None:
        """Store an object under `key`; returns its reference, or None if it could not be stored."""
        try:
            if self.bucket and self._client:
                self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
                return f"s3://{self.bucket}/{key}"
            if self.root:
                path = self.root / key
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                return f"file://{key}"
        except Exception:
            return None
        return None

    def put_json(self, key: str, obj) -> str | None:
        return self.put(key, json.dumps(obj, sort_keys=True, default=str).encode(), "application/json")
