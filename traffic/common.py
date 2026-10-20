"""Shared helpers: boto3 session, stack outputs, and JSON run records."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import REPO_ROOT

RUNS_DIR = REPO_ROOT / "runs"
RESULTS_DIR = REPO_ROOT / "results"


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def session(cfg: dict[str, Any], profile: str | None = None):
    """boto3 session from --profile or the standard credential chain; Region always from config."""
    import boto3

    return boto3.Session(profile_name=profile, region_name=cfg["region"])


def stack_outputs(sess, stack_name: str) -> dict[str, str]:
    """Reads outputs from CloudFormation DescribeStacks — never from hand-copied values."""
    cfn = sess.client("cloudformation")
    stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def lab_outputs(sess, cfg: dict[str, Any]) -> dict[str, Any]:
    raw = stack_outputs(sess, cfg["stackNames"]["lab"])
    outputs: dict[str, Any] = dict(raw)
    outputs["TeamRoleArns"] = json.loads(raw["TeamRoleArns"])
    # Keep a local copy so teardown_check.py can still find names after the stack is gone.
    write_json(RESULTS_DIR / "stack-outputs.json", {"recordedAt": utc_iso(), "outputs": outputs})
    return outputs


def write_json(path: Path, record: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"writtenAt": utc_iso(), **record}
    path.write_text(json.dumps(body, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None) or {}
    return response.get("Error", {}).get("Code", type(exc).__name__)
