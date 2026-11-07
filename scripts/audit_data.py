"""Synthetic-data-only check of the dataset manifest (PRD NFR-5, verified in E6).

    python scripts/audit_data.py                 # exit code 1 if anything fails

Passes when: every dataset in evaluation/manifest.json is declared synthetic, or is a network measurement that says it holds no
personal data; every file under evaluation/ and traces/ is listed in the manifest; no listed file contains anything that looks like
personal data (e-mail address, phone number, national-id or medical-record-number pattern, date of birth).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PII = {
    "e-mail address": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "phone number": re.compile(r"(?<!\d)(?:\+?\d{1,3}[ -])?(?:\(?\d{3}\)?[ -]?)\d{3}[ -]\d{4}(?!\d)"),
    "national id": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "medical record number": re.compile(r"(?i)\b(?:mrn|medical record(?: number)?)\s*[:#]?\s*\d{4,}"),
    "date of birth": re.compile(r"(?i)\b(?:dob|date of birth)\b"),
}


def audit(root: Path = ROOT) -> list[str]:
    problems = []
    mpath = root / "evaluation" / "manifest.json"
    if not mpath.exists():
        return ["evaluation/manifest.json is missing"]
    manifest = json.loads(mpath.read_text())
    listed = set()
    for d in manifest["datasets"]:
        listed.add(d["path"])
        if d.get("real_patient_data"):
            problems.append(f"{d['path']}: declares real patient data")
        elif not (d.get("synthetic") is True or d.get("contains_personal_data") is False):
            problems.append(f"{d['path']}: must be synthetic, or state that it holds no personal data")
        if not (root / d["path"]).exists():
            problems.append(f"{d['path']}: listed but not found")
    for base in ("evaluation", "traces"):
        for f in sorted((root / base).rglob("*")) if (root / base).exists() else []:
            rel = f.relative_to(root).as_posix()
            if f.is_file() and rel not in listed and f.name != "manifest.json":
                problems.append(f"{rel}: not listed in the manifest")
    for path in sorted(listed):
        f = root / path
        if f.is_file():
            text = f.read_text(encoding="utf-8", errors="ignore")
            for kind, pat in PII.items():
                m = pat.search(text)
                if m:
                    problems.append(f"{path}: looks like it contains a {kind} ({m.group(0)[:20]!r})")
    return problems


def main(argv=None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    problems = audit()
    print("RESULT: PASS" if not problems else "RESULT: FAIL\n" + "\n".join(f"- {p}" for p in problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
