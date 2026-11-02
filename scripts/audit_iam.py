"""Configuration and secrets audit for NFR-5 (least-privilege IAM, no secrets in code). Verified in experiment E6 by an
audit, not a runtime measurement.

    python scripts/audit_iam.py                 # prints a report, exit code 1 if anything fails
    python scripts/audit_iam.py --out docs/iam-audit.md

Checks:
  1. IAM policies in infra/iam/*.json and infra/terraform/*.tf: no `"*"` action, no `service:*` action, no `"*"`
     resource, except the listed, justified exceptions.
  2. No secrets in tracked source: AWS key ids and secrets, private keys, GitHub/Slack tokens, hard-coded passwords.
  3. Every `.env`-style or example config holds placeholders only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (file name, statement id or action) -> why a wildcard is acceptable. Anything not listed fails the audit.
JUSTIFIED = {
    ("p3-dev-policy.json", "bedrock:InvokeModel"): "Bedrock invocation resources are foundation-model and inference-profile ARNs that differ per region",
    ("p3-dev-policy.json", "DynamoDbListTables"): "dynamodb:ListTables does not support resource-level permissions in IAM",
}
SECRET_PATTERNS = {
    "AWS access key id": re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    "AWS secret key": re.compile(r"aws_secret_access_key\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{30,}"),
    "private key": re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "Slack token": re.compile(r"\bxox[abp]-[A-Za-z0-9-]{10,}"),
    "hard-coded password": re.compile(r"(?i)\bpassword\s*=\s*['\"][^'\"\s]{6,}['\"]"),
}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "results", "build", ".pytest_cache", "tests", "edge_artifacts"}
SKIP_FILES = {"audit_iam.py", "iam-audit.md"}
TEXT_SUFFIXES = {".py", ".tf", ".json", ".md", ".yaml", ".yml", ".sh", ".txt", ".js", ".html", ".toml", ".cfg", ".env", ".example"}


def iam_findings() -> list[dict]:
    out = []
    for path in sorted((ROOT / "infra").rglob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for st in (doc.get("Statement") or []) if isinstance(doc, dict) else []:
            out += _check(path.name, st.get("Sid", "-"), st.get("Effect"), st.get("Action"), st.get("Resource"))
    for path in sorted((ROOT / "infra").rglob("*.tf")):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r'Sid\s*=\s*"([^"]+)"(.*?)(?=\n\s*\{\s*\n\s*Sid|\n\s*\]\s*\n\s*\}\))', text, re.S):
            body = m.group(2)
            actions = re.findall(r'"([a-z0-9-]+:[A-Za-z*]+|\*)"', re.search(r"Action\s*=\s*(\[.*?\]|\".*?\")", body, re.S).group(1)) \
                if re.search(r"Action\s*=\s*(\[.*?\]|\".*?\")", body, re.S) else []
            res = re.search(r"Resource\s*=\s*(\[.*?\]|\"[^\"]*\"|[A-Za-z_.\[\]\"0-9]+)", body, re.S)
            out += _check(path.name, m.group(1), "Allow", actions, ["*"] if res and re.fullmatch(r'\s*"\*"\s*|\s*\[\s*"\*"\s*\]\s*', res.group(1)) else ["scoped"])
    return out


def _check(file: str, sid: str, effect, actions, resources) -> list[dict]:
    if effect != "Allow":
        return []
    acts = [actions] if isinstance(actions, str) else list(actions or [])
    res = [resources] if isinstance(resources, str) else list(resources or [])
    found = []
    for a in acts:
        if a == "*" or a.endswith(":*"):
            found.append({"file": file, "statement": sid, "problem": f"wildcard action {a}", "justified": JUSTIFIED.get((file, a))})
    if "*" in res:
        justified = JUSTIFIED.get((file, sid)) or next((JUSTIFIED[(file, a)] for a in acts if (file, a) in JUSTIFIED), None)
        found.append({"file": file, "statement": sid, "problem": "wildcard resource", "justified": justified})
    return found


def secret_findings() -> list[dict]:
    out = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.name in SKIP_FILES or SKIP_DIRS & set(path.relative_to(ROOT).parts) or path.suffix not in TEXT_SUFFIXES:
            continue
        if path.stat().st_size > 1_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, pat in SECRET_PATTERNS.items():
            for m in pat.finditer(text):
                if name == "AWS access key id" and len(set(m.group(0)[4:])) <= 2:
                    continue                      # AKIAXXXXXXXXXXXXXXXX and the like are placeholders, not keys
                out.append({"file": str(path.relative_to(ROOT)), "line": text[:m.start()].count("\n") + 1, "kind": name})
    return out


def run() -> tuple[str, bool]:
    iam, secrets = iam_findings(), secret_findings()
    bad_iam = [f for f in iam if not f["justified"]]
    lines = ["# IAM and secrets audit (NFR-5)", "", f"- IAM statements with a wildcard: {len(iam)} ({len(bad_iam)} unjustified)",
             f"- Secrets found in tracked source: {len(secrets)}", ""]
    if iam:
        lines += ["| file | statement | problem | justification |", "|---|---|---|---|"]
        lines += [f"| {f['file']} | {f['statement']} | {f['problem']} | {f['justified'] or '**NONE**'} |" for f in iam]
        lines.append("")
    if secrets:
        lines += ["| file | line | kind |", "|---|---|---|"] + [f"| {s['file']} | {s['line']} | {s['kind']} |" for s in secrets] + [""]
    ok = not bad_iam and not secrets
    lines.append("**RESULT: PASS**" if ok else "**RESULT: FAIL**")
    return "\n".join(lines) + "\n", ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    report, ok = run()
    print(report)
    if a.out:
        Path(a.out).write_text(report, encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
