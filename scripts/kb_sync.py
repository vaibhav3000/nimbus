"""Prepare (and optionally upload) a pack's rule documents for a Bedrock Knowledge Base data source.

    python scripts/kb_sync.py --pack hospital_sim --out build/kb            # write markdown + metadata sidecars locally
    python scripts/kb_sync.py --pack hospital_sim --bucket my-bucket --prefix nimbus/kb   # also upload to S3

Each rule becomes `<id>.md` with a `<id>.md.metadata.json` sidecar (policy_id, title). Point the Knowledge Base data source
at s3://<bucket>/<prefix>/ and set NIMBUS_KB_ID for the service. Nothing is uploaded unless --bucket is given. The rules
the system ENFORCES stay in the pack; the Knowledge Base only supplies the texts the model reads (see cloud/retrieval.py).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cloud.pack import Pack  # noqa: E402


def documents(pack: Pack) -> list[tuple[str, str, dict]]:
    out = []
    for p in pack.policy_docs:
        body = f"# {p['title']}\n\n{p['text']}\n\nKeywords: {', '.join(p.get('keywords', []))}\n"
        out.append((f"{p['id']}.md", body, {"metadataAttributes": {"policy_id": p["id"], "title": p["title"]}}))
    return out


def main(argv=None, client=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pack", default="hospital_sim")
    ap.add_argument("--out", default=str(ROOT / "build" / "kb"))
    ap.add_argument("--bucket")
    ap.add_argument("--prefix", default="nimbus/kb")
    a = ap.parse_args(argv)
    docs = documents(Pack.load(a.pack))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, body, meta in docs:
        (out / name).write_text(body, encoding="utf-8")
        (out / f"{name}.metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    print(f"wrote {len(docs)} documents to {out}")
    if a.bucket:
        if client is None:
            import boto3
            client = boto3.client("s3")
        for name, body, meta in docs:
            client.put_object(Bucket=a.bucket, Key=f"{a.prefix}/{name}", Body=body.encode(), ContentType="text/markdown")
            client.put_object(Bucket=a.bucket, Key=f"{a.prefix}/{name}.metadata.json", Body=json.dumps(meta).encode(), ContentType="application/json")
        print(f"uploaded {len(docs) * 2} objects to s3://{a.bucket}/{a.prefix}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
