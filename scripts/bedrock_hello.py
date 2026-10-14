"""Step 1 of connecting to AWS: prove Bedrock Converse + tool use works from
this machine/region, and find a model you can actually invoke.

    python scripts/bedrock_hello.py --list                      # what is available here
    python scripts/bedrock_hello.py --model <model-or-profile-id>   # one tiny tool-use call

Costs a fraction of a cent. Needs AWS credentials (aws configure) and Bedrock
model access enabled in the console for the model you pick.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3  # noqa: E402

from cloud.pack import Pack  # noqa: E402
from cloud.tools import ToolRegistry  # noqa: E402


def list_models(region: str) -> None:
    b = boto3.client("bedrock", region_name=region)
    print(f"Region {region}: text models that stream or take tools (check access in the console):")
    for m in b.list_foundation_models(byOutputModality="TEXT")["modelSummaries"]:
        print(f"  {m['modelId']:70} {m['providerName']}")
    try:
        profiles = b.list_inference_profiles()["inferenceProfileSummaries"]
        print("\nCross-region inference profiles (use these ids when a model is not offered in-region):")
        for p in profiles:
            print(f"  {p['inferenceProfileId']:70} {p['inferenceProfileName']}")
    except Exception as exc:  # older accounts/regions
        print(f"\n(inference profiles unavailable: {exc})")


def hello(model: str, region: str) -> None:
    c = boto3.client("bedrock-runtime", region_name=region)
    t0 = time.perf_counter()
    resp = c.converse(
        modelId=model,
        system=[{"text": "You are a hospital logistics assistant. Always answer by calling a tool."}],
        messages=[{"role": "user", "content": [{"text": "Need a pump somewhere on the 2nd floor."}]}],
        toolConfig=ToolRegistry(Pack.load()).bedrock_config(["submit_task_spec", "ask_clarification"]),
        inferenceConfig={"maxTokens": 300, "temperature": 0},
    )
    ms = int((time.perf_counter() - t0) * 1000)
    calls = [b["toolUse"] for b in resp["output"]["message"]["content"] if "toolUse" in b]
    print(f"model {model}  {ms} ms  usage {resp['usage']}")
    print("tool calls:", json.dumps([{"name": c_["name"], "input": c_["input"]} for c_ in calls], indent=1))
    ok = bool(calls) and calls[0]["name"] == "ask_clarification"
    print("RESULT:", "tool use works; the model asked a clarifying question as it should" if ok
          else "tool use returned something unexpected: try another model")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="ap-south-1")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--model")
    a = ap.parse_args()
    if a.list:
        list_models(a.region)
    elif a.model:
        hello(a.model, a.region)
    else:
        ap.error("pass --list or --model")
