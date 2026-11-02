"""FR-19 rule retrieval: Knowledge Base when configured, keywords otherwise and on failure; restrictions always cited."""

import json
import sys
from pathlib import Path

from cloud.pack import Pack
from cloud.policy import PolicyBook
from cloud.retrieval import BedrockKBRetriever

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import kb_sync  # noqa: E402


class StubKB:
    def __init__(self, results=None, boom=False):
        self.results, self.boom, self.calls = results or [], boom, []

    def retrieve(self, **kw):
        self.calls.append(kw)
        if self.boom:
            raise RuntimeError("ThrottlingException")
        return {"retrievalResults": self.results}


def book(kb=None):
    b = PolicyBook(Pack.load("hospital"))
    b.retriever = BedrockKBRetriever("KB123", client=kb) if kb is not None else None
    return b


def test_default_is_keyword_retrieval_with_no_aws_call():
    docs = book().retrieve("infusion pump to a paediatric bed")
    assert docs and docs[0]["id"] == "SOP-PAED-01" and "source" not in docs[0]


def test_knowledge_base_results_are_used_when_configured():
    kb = StubKB([{"content": {"text": "# Gate rule\nOne robot at a time through a gate."}, "metadata": {"policy_id": "SOP-GATE-03", "title": "Single-lane gates"}}])
    docs = book(kb).retrieve("robot through a gate", limit=2)
    assert docs[0]["id"] == "SOP-GATE-03" and docs[0]["source"] == "knowledge-base"
    assert kb.calls[0]["knowledgeBaseId"] == "KB123" and kb.calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]["numberOfResults"] == 2


def test_failure_or_empty_result_falls_back_to_keywords():
    assert book(StubKB(boom=True)).retrieve("pump paediatric")[0]["id"] == "SOP-PAED-01"
    assert book(StubKB([])).retrieve("pump paediatric")[0]["id"] == "SOP-PAED-01"


def test_an_active_restriction_is_always_in_front_of_the_model_whatever_retrieval_returned():
    b = book(StubKB([{"content": {"text": "Something unrelated"}, "metadata": {}}]))
    b.declare("2C", "isolation")
    ids = [d["id"] for d in b.retrieve("deliver a blanket")]
    assert "SOP-ISO-02" in ids


def test_kb_sync_writes_documents_with_metadata_and_uploads_only_when_asked(tmp_path):
    assert kb_sync.main(["--pack", "hospital", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "SOP-PAED-01.md").read_text().startswith("# Paediatric ward equipment")
    assert json.loads((tmp_path / "SOP-PAED-01.md.metadata.json").read_text())["metadataAttributes"]["policy_id"] == "SOP-PAED-01"

    class S3:
        puts = []
        def put_object(self, **kw): self.puts.append(kw["Key"])
    s3 = S3()
    kb_sync.main(["--pack", "hospital", "--out", str(tmp_path), "--bucket", "b", "--prefix", "p"], client=s3)
    assert "p/SOP-PAED-01.md" in s3.puts and "p/SOP-PAED-01.md.metadata.json" in s3.puts
