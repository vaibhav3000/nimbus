# Nimbus Context: Read This First

Welcome to the Nimbus Person 4 (P4) repository for DS252 Team Nimbus. This repository is owned by Person 4 (Vaibhav).

Before making any changes or editing any files in this repository, you must read the documents in this `context/` directory.

## What This Repository Is

This repository contains the shared foundation for Project Nimbus (PRD v5.0):
- Wire contracts for communication across cloud, edge, and robot tiers.
- Plain-language requester web application (FR-21).
- Replayable, append-only decision log (FR-20) with timeline replay.
- Thin local API server with a cloud stand-in that runs end-to-end without an AWS account.
- Terraform skeleton for target AWS infrastructure (PRD Fig 4).
- PRD Section 7 cost counters tracking each term of the cost formula.
- Cloud-edge messaging outbox stand-in and check-in ingress.
- Minimal operator console for recording control intents.

## How to Run

1. Run the local application server:
```bash
python server.py
```
Open http://127.0.0.1:8000 in your browser. You can submit requests such as "Need an infusion pump at 2B-12, urgent." and follow the decision timeline.

2. Run the automated test suite:
```bash
pip install -r requirements.txt
python -m pytest tests/ -q
```
All tests test public HTTP behavior and contract schemas.

3. Validate the Terraform skeleton:
```bash
cd infra/terraform
terraform init -backend=false
terraform validate
```
Validation passes cleanly. Never run `terraform apply` without explicit authorization from the AWS account owner.

## Golden Rules for Agents

- Read all files in `context/` before proposing or making changes.
- Do not modify wire contracts in `contracts/` without team coordination.
- Do not implement tracks assigned to other teammates (Person 1, 2, or 3).
- Adhere strictly to the ponytail discipline: build the simplest thing that works, prefer standard library, and keep diffs small.
- All testing must go through public HTTP endpoints and schemas, preserving the public seam.
- Never apply Terraform infrastructure.
