"""Converse calls with requestMetadata, throttling backoff, and one JSONL line per call."""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any

from .common import error_code, utc_iso
from .prompts import prompt_for

# Transient errors the Bedrock docs say to retry with exponential backoff and jitter:
# - ThrottlingException (429), InternalServerException (500) and ServiceUnavailableException (503):
#   https://docs.aws.amazon.com/bedrock/latest/userguide/troubleshooting-api-error-codes.html
#   (the Converse API reference links the last two to its InternalFailure and ServiceUnavailable sections)
# - ModelNotReadyException (429): "The AWS SDK will automatically retry the operation up to 5 times",
#   https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html
# Everything else (validation, access, not found, model timeout) fails straight away.
RETRYABLE = frozenset({
    "ThrottlingException",
    "InternalServerException",
    "ServiceUnavailableException",
    "ModelNotReadyException",
})

# Converse API reference: at most 16 entries; key [a-zA-Z0-9\s:_@$#=/+,-.]{1,256}, value {0,256}.
MAX_METADATA_ENTRIES = 16
_METADATA_KEY = re.compile(r"[a-zA-Z0-9\s:_@$#=/+,\-.]{1,256}")
_METADATA_VALUE = re.compile(r"[a-zA-Z0-9\s:_@$#=/+,\-.]{0,256}")


def check_metadata(meta: dict[str, str]) -> dict[str, str]:
    if len(meta) > MAX_METADATA_ENTRIES:
        raise ValueError(f"requestMetadata has {len(meta)} entries; the limit is {MAX_METADATA_ENTRIES}")
    for k, v in meta.items():
        if not _METADATA_KEY.fullmatch(k) or not _METADATA_VALUE.fullmatch(v):
            raise ValueError(f"requestMetadata entry {k!r}={v!r} uses characters or a length Bedrock rejects")
    return meta


def request_metadata(cfg: dict[str, Any], team: str, feature: str, run_id: str, mode: str,
                     user: str | None = None) -> dict[str, str]:
    meta = {
        "team": team,
        "feature": feature,
        "run_id": run_id,
        "mode": mode,
        "lab": cfg["traffic"]["metadataLab"],
    }
    if user:
        meta["user"] = user
    return check_metadata(meta)


def model_for(cfg: dict[str, Any], outputs: dict[str, Any], feature: str) -> str:
    """Calls for profile.routeFeature go through the application inference profile."""
    if feature == cfg["profile"]["routeFeature"]:
        return outputs["ProfileArn"]
    return cfg["model"]["foundationModelId"]


def converse_with_backoff(client, max_attempts: int, sleep=time.sleep, **kwargs) -> dict[str, Any]:
    """Exponential backoff with full jitter on the RETRYABLE errors, up to max_attempts tries."""
    for attempt in range(1, max_attempts + 1):
        try:
            return client.converse(**kwargs)
        except Exception as exc:  # botocore ClientError
            if error_code(exc) not in RETRYABLE or attempt == max_attempts:
                raise
            sleep(random.uniform(0, min(20.0, 0.5 * 2 ** attempt)))
    raise RuntimeError("unreachable")


def invoke_once(client, cfg: dict[str, Any], *, model_id: str, team: str, feature: str, run_id: str,
                mode: str, index: int, user: str | None = None) -> dict[str, Any]:
    started = time.monotonic()
    record: dict[str, Any] = {
        "timestamp": utc_iso(),
        "mode": mode,
        "team": team,
        "feature": feature,
        "user": user,
        "modelId": model_id,
    }
    try:
        resp = converse_with_backoff(
            client,
            int(cfg["traffic"]["maxAttempts"]),
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": prompt_for(feature, index)}]}],
            inferenceConfig={"maxTokens": int(cfg["model"]["maxTokens"])},
            requestMetadata=request_metadata(cfg, team, feature, run_id, mode, user),
        )
        usage = resp.get("usage", {})
        record.update(
            requestId=resp.get("ResponseMetadata", {}).get("RequestId"),
            inputTokens=usage.get("inputTokens"),
            outputTokens=usage.get("outputTokens"),
            error=None,
        )
    except Exception as exc:
        record.update(
            requestId=(getattr(exc, "response", None) or {}).get("ResponseMetadata", {}).get("RequestId"),
            inputTokens=None,
            outputTokens=None,
            error=f"{error_code(exc)}: {exc}",
        )
    record["latencyMs"] = round((time.monotonic() - started) * 1000)
    return record


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
