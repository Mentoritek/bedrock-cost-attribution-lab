"""Offline tests for the traffic package (spec section 9.1, item 3). No AWS calls."""

import copy
import os
import subprocess
import sys
from pathlib import Path

import pytest
from botocore.credentials import Credentials

from traffic.chat_completions_check import SIGNING_SERVICE, build_signed_request, endpoint_url, to_urllib
from traffic.config import DEFAULT_CONFIG_PATH, REPO_ROOT, ConfigError, config_path, load_config, validate
from traffic.invoke import RETRYABLE, check_metadata, converse_with_backoff, request_metadata
from traffic.redact import MASK, redact
from traffic.schedule import assign_users, build_plan, feature_counts, plan_table, team_counts

FAKE_ACCOUNT = "111111111111"


@pytest.fixture
def cfg():
    return load_config(DEFAULT_CONFIG_PATH)  # committed file, never the local override


# ---------------------------------------------------------------- config file precedence

def test_config_path_precedence(tmp_path, monkeypatch):
    local = tmp_path / "lab.config.local.json"
    monkeypatch.delenv("LAB_CONFIG", raising=False)
    assert config_path(local=local) == DEFAULT_CONFIG_PATH  # no local file yet
    local.write_text("{}")
    assert config_path(local=local) == local  # local file wins over the default
    monkeypatch.setenv("LAB_CONFIG", str(tmp_path / "env.json"))
    assert config_path(local=local) == (tmp_path / "env.json").resolve()  # $LAB_CONFIG wins over local
    assert config_path("explicit.json", local=local) == Path("explicit.json").resolve()  # --config wins


# ---------------------------------------------------------------- schedule

def test_team_counts_exact_for_default_config(cfg):
    assert team_counts(cfg["teams"], 600) == {"search": 420, "support": 120, "analytics": 60}


def test_largest_remainder_sums_to_total(cfg):
    for total in (5, 7, 13, 101, 599, 601):
        assert sum(team_counts(cfg["teams"], total).values()) == total


def test_largest_remainder_gives_leftover_to_biggest_fraction():
    teams = [{"name": "a", "weight": 0.5}, {"name": "b", "weight": 0.3}, {"name": "c", "weight": 0.2}]
    # 7 x weights = 3.5, 2.1, 1.4 -> floors 3, 2, 1, leftover 1 goes to a (0.5 remainder)
    assert team_counts(teams, 7) == {"a": 4, "b": 2, "c": 1}


def test_feature_split_remainder_to_first():
    assert feature_counts(["x", "y"], 5) == {"x": 3, "y": 2}
    assert feature_counts(["only"], 4) == {"only": 4}


def test_plan_counts_exact(cfg):
    counts = {(r["team"], r["feature"]): r["calls"] for r in plan_table(build_plan(cfg))}
    assert counts == {
        ("search", "query-rewrite"): 210,
        ("search", "result-summary"): 210,
        ("support", "ticket-triage"): 60,
        ("support", "reply-draft"): 60,
        ("analytics", "report-narrative"): 60,
    }


def test_plan_is_deterministic_for_a_seed(cfg):
    assert build_plan(cfg) == build_plan(cfg)
    assert build_plan(cfg, seed=1) != build_plan(cfg, seed=2)
    assert plan_table(build_plan(cfg, seed=1)) == plan_table(build_plan(cfg, seed=2))


def test_round_robin_user_mapping(cfg):
    plan = build_plan(cfg, calls=40)
    users = assign_users(plan, cfg["gateway"]["users"])
    search_users = [u["session"] for c, u in zip(plan, users, strict=True) if c.team == "search"]
    assert search_users[:4] == ["alice", "bob", "alice", "bob"]
    for call, user in zip(plan, users, strict=True):
        assert user["team"] == call.team


def test_round_robin_needs_a_user_per_team(cfg):
    plan = build_plan(cfg, calls=40)
    with pytest.raises(ValueError):
        assign_users(plan, [u for u in cfg["gateway"]["users"] if u["team"] != "analytics"])


# ---------------------------------------------------------------- config validation

def test_weights_must_sum_to_one(cfg):
    bad = copy.deepcopy(cfg)
    bad["teams"][0]["weight"] = 0.9
    with pytest.raises(ConfigError, match="weights"):
        validate(bad)


def test_weights_tolerance(cfg):
    ok = copy.deepcopy(cfg)
    ok["teams"][0]["weight"] += 0.0009
    validate(ok)


def test_gateway_user_team_must_exist(cfg):
    bad = copy.deepcopy(cfg)
    bad["gateway"]["users"][0]["team"] = "nope"
    with pytest.raises(ConfigError, match="unknown team"):
        validate(bad)


def test_route_feature_must_belong_to_a_team(cfg):
    bad = copy.deepcopy(cfg)
    bad["profile"]["routeFeature"] = "nope"
    with pytest.raises(ConfigError, match="routeFeature"):
        validate(bad)


def test_tag_keys_distinct_and_non_empty(cfg):
    bad = copy.deepcopy(cfg)
    bad["tagKeys"]["path"] = bad["tagKeys"]["team"]
    with pytest.raises(ConfigError, match="distinct"):
        validate(bad)
    bad["tagKeys"]["path"] = " "
    with pytest.raises(ConfigError, match="non-empty"):
        validate(bad)


def test_calls_per_run_at_least_features(cfg):
    bad = copy.deepcopy(cfg)
    bad["traffic"]["callsPerRun"] = 2
    with pytest.raises(ConfigError, match="callsPerRun"):
        validate(bad)


# ---------------------------------------------------------------- redaction

def test_redaction_masks_12_digit_numbers():
    text = f"arn:aws:iam::{FAKE_ACCOUNT}:role/BedrockLab-Team-search account {FAKE_ACCOUNT}"
    assert redact(text, show_ids=False) == f"arn:aws:iam::{MASK}:role/BedrockLab-Team-search account {MASK}"
    assert redact(text, show_ids=True) == text


def test_redaction_leaves_other_numbers():
    assert redact("1234567890123 12345678901 600 calls", show_ids=False) == "1234567890123 12345678901 600 calls"


# ---------------------------------------------------------------- request metadata and backoff

def test_request_metadata_fields(cfg):
    meta = request_metadata(cfg, "support", "reply-draft", "day1-gw-peruser", "gateway-per-user", "carol@example.com")
    assert meta == {"team": "support", "feature": "reply-draft", "run_id": "day1-gw-peruser",
                    "mode": "gateway-per-user", "lab": "mt-01", "user": "carol@example.com"}


def test_request_metadata_rejects_bad_characters():
    with pytest.raises(ValueError):
        check_metadata({"team": "search!"})
    with pytest.raises(ValueError):
        check_metadata({f"k{i}": "v" for i in range(17)})


def test_backoff_retries_throttling_then_succeeds():
    class Throttle(Exception):
        response = {"Error": {"Code": "ThrottlingException"}}

    calls = {"n": 0}

    class Client:
        def converse(self, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise Throttle()
            return {"ok": True}

    assert converse_with_backoff(Client(), 6, sleep=lambda s: None) == {"ok": True}
    assert calls["n"] == 3


def _failing_client(codes):
    """converse() raises one ClientError-shaped exception per code in `codes`, then succeeds."""
    class Failure(Exception):
        def __init__(self, code):
            super().__init__(code)
            self.response = {"Error": {"Code": code}}

    class Client:
        calls = 0

        def converse(self, **kwargs):
            Client.calls += 1
            if Client.calls <= len(codes):
                raise Failure(codes[Client.calls - 1])
            return {"ok": True}

    return Client()


@pytest.mark.parametrize("code", sorted(RETRYABLE))
def test_backoff_retries_documented_transient_errors(code):
    # Deploy log entry 11: one InternalServerException ("Try your request again") failed a call unretried.
    sleeps = []
    client = _failing_client([code, code])
    assert converse_with_backoff(client, 6, sleep=sleeps.append) == {"ok": True}
    assert type(client).calls == 3 and len(sleeps) == 2


@pytest.mark.parametrize("code", ["ValidationException", "AccessDeniedException", "ResourceNotFoundException",
                                  "ModelTimeoutException", "ModelErrorException"])
def test_backoff_does_not_retry_other_errors(code):
    client = _failing_client([code])
    with pytest.raises(Exception, match=code):
        converse_with_backoff(client, 6, sleep=lambda s: None)
    assert type(client).calls == 1


def test_retryable_codes_are_converse_errors_in_botocore():
    from botocore.session import get_session

    op = get_session().get_service_model("bedrock-runtime").operation_model("Converse")
    assert RETRYABLE <= {shape.name for shape in op.error_shapes}


def test_backoff_gives_up_after_max_attempts():
    class Throttle(Exception):
        response = {"Error": {"Code": "ThrottlingException"}}

    class Client:
        def converse(self, **kwargs):
            raise Throttle()

    with pytest.raises(Throttle):
        converse_with_backoff(Client(), 6, sleep=lambda s: None)


def test_converse_request_matches_botocore_model(cfg):
    from botocore.session import get_session
    from botocore.validate import validate_parameters

    from traffic.prompts import prompt_for

    shape = get_session().get_service_model("bedrock-runtime").operation_model("Converse").input_shape
    validate_parameters({
        "modelId": cfg["model"]["foundationModelId"],
        "messages": [{"role": "user", "content": [{"text": prompt_for("reply-draft", 0)}]}],
        "inferenceConfig": {"maxTokens": cfg["model"]["maxTokens"]},
        "requestMetadata": request_metadata(cfg, "support", "reply-draft", "r1", "direct"),
    }, shape)


# ---------------------------------------------------------------- Chat Completions (T3)

def test_sigv4_request_for_bedrock_and_documented_url(cfg):
    fake = Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "FAKE-SESSION-TOKEN")
    req = build_signed_request(fake, cfg["region"], cfg["model"]["foundationModelId"], 16)
    assert SIGNING_SERVICE == "bedrock"
    assert req.url == "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/chat/completions"
    assert endpoint_url("us-east-1") == req.url
    assert req.method == "POST"
    auth = req.headers["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/us-east-1/bedrock/aws4_request" in auth
    assert req.headers["X-Amz-Security-Token"] == "FAKE-SESSION-TOKEN"
    assert isinstance(req.data, bytes)
    assert b'"model": "openai.gpt-oss-20b-1:0"' in req.data


def test_signed_request_passes_urllib_body_check(cfg):
    """Regression: a str body raised TypeError in urllib's do_request_ before anything was sent."""
    import urllib.request

    fake = Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "FAKE-SESSION-TOKEN")
    req = to_urllib(build_signed_request(fake, cfg["region"], cfg["model"]["foundationModelId"], 16))
    opener = urllib.request.build_opener()
    handler = next(h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler))
    checked = handler.do_request_(req)  # the check that failed; prepares the request, no network call
    assert isinstance(checked.data, bytes)
    assert checked.get_header("Content-length") == str(len(checked.data))


# ---------------------------------------------------------------- CLI

def test_dry_run_prints_plan_without_credentials():
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env.update(AWS_CONFIG_FILE="/nonexistent", AWS_SHARED_CREDENTIALS_FILE="/nonexistent",
               LAB_CONFIG=str(DEFAULT_CONFIG_PATH))
    result = subprocess.run(
        [sys.executable, "-m", "traffic", "run", "--mode", "direct", "--dry-run"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "query-rewrite" in result.stdout and "210" in result.stdout
    assert "SAMPLE DATA" in result.stdout
