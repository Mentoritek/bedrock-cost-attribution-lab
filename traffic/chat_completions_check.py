"""T3: Chat Completions naming the foundation model (control) vs naming the application inference profile.

Both requests are SigV4-signed with botocore and sent to the bedrock-runtime OpenAI-compatible
endpoint. The script records what comes back; it asserts no outcome.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

SIGNING_SERVICE = "bedrock"
SAMPLE_MESSAGE = "[SAMPLE DATA - MT-01 lab] Reply with the single word: ok."


def endpoint_url(region: str) -> str:
    return f"https://bedrock-runtime.{region}.amazonaws.com/openai/v1/chat/completions"


def build_signed_request(credentials, region: str, model: str, max_tokens: int) -> AWSRequest:
    """credentials: a botocore Credentials object (fake ones in tests; nothing is sent here)."""
    body = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": SAMPLE_MESSAGE}], "max_tokens": max_tokens}
    )
    request = AWSRequest(
        method="POST",
        url=endpoint_url(region),
        data=body,
        headers={"Content-Type": "application/json"},
    )
    SigV4Auth(credentials, SIGNING_SERVICE, region).add_auth(request)
    return request


def send(request: AWSRequest, timeout: float = 60.0) -> dict[str, Any]:
    prepared = request.prepare()
    req = urllib.request.Request(prepared.url, data=prepared.body, headers=dict(prepared.headers),
                                 method=prepared.method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return {"status": resp.status, "body": _parse(raw), "error": None}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return {"status": exc.code, "body": _parse(raw), "error": raw}
    except urllib.error.URLError as exc:
        return {"status": None, "body": None, "error": str(exc.reason)}


def _parse(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def run_check(credentials, region: str, foundation_model_id: str, profile_arn: str,
              max_tokens: int) -> dict[str, Any]:
    results = {}
    for label, model in (("foundationModel", foundation_model_id), ("applicationInferenceProfile", profile_arn)):
        response = send(build_signed_request(credentials, region, model, max_tokens))
        results[label] = {"model": model, **response}
    return {"endpoint": endpoint_url(region), "signingService": SIGNING_SERVICE, "results": results}
