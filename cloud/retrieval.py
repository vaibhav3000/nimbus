"""Rule retrieval for the model's context (PRD FR-19, "rule knowledge retrieval").

`PolicyBook.retrieve` keeps its keyword scoring (offline, free, deterministic: the default). When a Bedrock Knowledge
Base is configured (`NIMBUS_KB_ID`), this retriever asks it for the passages closest to the request instead, falling
back to the keyword result if the call fails or returns nothing. Documents for restrictions that are active are always
included, whichever retriever ran: a safety rule must not depend on a similarity score.

The rules themselves are enforced by the Rule Checker from the pack, never by what retrieval returned; retrieval only
decides which rule texts the model gets to read.
"""

from __future__ import annotations

import os


class BedrockKBRetriever:
    def __init__(self, kb_id: str, *, client=None, region: str = "ap-south-1", timeout_s: float = 4.0):
        self.kb_id = kb_id
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("bedrock-agent-runtime", region_name=region,
                                  config=Config(connect_timeout=2, read_timeout=timeout_s, retries={"max_attempts": 1}))
        self.client = client

    @classmethod
    def from_env(cls) -> "BedrockKBRetriever | None":
        kb = os.environ.get("NIMBUS_KB_ID")
        return cls(kb, region=os.environ.get("AWS_REGION", "ap-south-1")) if kb else None

    def retrieve(self, text: str, limit: int = 3) -> list[dict]:
        resp = self.client.retrieve(
            knowledgeBaseId=self.kb_id, retrievalQuery={"text": text[:1000]},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": limit}})
        out = []
        for i, r in enumerate(resp.get("retrievalResults", [])):
            body = (r.get("content") or {}).get("text", "").strip()
            if not body:
                continue
            meta = r.get("metadata") or {}
            first = body.splitlines()[0].lstrip("# ").strip()
            out.append({"id": str(meta.get("policy_id") or f"KB-{i + 1}"), "title": str(meta.get("title") or first)[:80],
                        "text": body[:600], "keywords": [], "source": "knowledge-base"})
        return out
