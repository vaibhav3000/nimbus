"""NFR-5 audit tooling: it passes on the repository and fails on a bad policy or a leaked key."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_iam  # noqa: E402


def test_repository_passes_the_audit_and_a_report_is_written(tmp_path):
    report, ok = audit_iam.run()
    assert ok, report
    out = tmp_path / "r.md"
    assert audit_iam.main(["--out", str(out)]) == 0 and "RESULT: PASS" in out.read_text()


def test_wildcards_in_json_and_terraform_are_caught(tmp_path, monkeypatch):
    (tmp_path / "infra" / "iam").mkdir(parents=True)
    (tmp_path / "infra" / "iam" / "bad.json").write_text(json.dumps({"Statement": [
        {"Sid": "Everything", "Effect": "Allow", "Action": "*", "Resource": "*"},
        {"Sid": "Svc", "Effect": "Allow", "Action": ["s3:*"], "Resource": "arn:aws:s3:::b/*"},
        {"Sid": "Fine", "Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "arn:aws:s3:::b/*"}]}))
    (tmp_path / "infra" / "x.tf").write_text('''
      Statement = [
        {
          Sid    = "TfBad"
          Effect = "Allow"
          Action = ["dynamodb:*"]
          Resource = "*"
        },
        {
          Sid    = "TfFine"
          Effect = "Allow"
          Action = ["sqs:ReceiveMessage"]
          Resource = aws_sqs_queue.q.arn
        },
      ]
    ''')
    monkeypatch.setattr(audit_iam, "ROOT", tmp_path)
    found = audit_iam.iam_findings()
    assert {(f["statement"], f["problem"]) for f in found} >= {("Everything", "wildcard action *"), ("Everything", "wildcard resource"),
                                                              ("Svc", "wildcard action s3:*"), ("TfBad", "wildcard action dynamodb:*")}
    assert not any(f["statement"] in ("Fine", "TfFine") for f in found)
    assert audit_iam.run()[1] is False


def test_secrets_are_caught_but_placeholders_and_tests_are_not(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text('KEY = "AKIAABCDEFGHIJKLMNOP"\nTOKEN = "ghp_' + "a" * 36 + '"\npassword = "hunter22hunter"\n')
    (tmp_path / "ok.py").write_text('KEY = os.environ["AWS_ACCESS_KEY_ID"]\nPLACEHOLDER = "AKIAXXXXXXXXXXXXXXXX"\n')
    (tmp_path / "tests").mkdir(); (tmp_path / "tests" / "t.py").write_text('K = "AKIAABCDEFGHIJKLMNOP"')
    monkeypatch.setattr(audit_iam, "ROOT", tmp_path)
    kinds = {(f["file"], f["kind"]) for f in audit_iam.secret_findings()}
    assert kinds == {("app.py", "AWS access key id"), ("app.py", "GitHub token"), ("app.py", "hard-coded password")}
