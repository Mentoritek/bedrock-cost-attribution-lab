"""Offline tests for scripts/ helpers and query files. No AWS calls."""

import re
from datetime import date

import activate_tags
import create_export_cli
import pytest
import verify
from botocore.session import get_session
from botocore.validate import validate_parameters

from traffic.config import DEFAULT_CONFIG_PATH, REPO_ROOT, load_config

QUERIES = REPO_ROOT / "queries" / "logs-insights"


@pytest.fixture
def cfg():
    return load_config(DEFAULT_CONFIG_PATH)  # committed file, never the local override


def input_shape(service, op):
    return get_session().get_service_model(service).operation_model(op).input_shape


# ---------------------------------------------------------------- Logs Insights queries

def test_team_feature_query_uses_documented_fields():
    q = (QUERIES / "tokens-by-team-feature.txt").read_text()
    for field in ("requestMetadata.team", "requestMetadata.feature", "modelId",
                  "input.inputTokenCount", "output.outputTokenCount", "requestMetadata.lab"):
        assert field in q


def test_principal_query_uses_documented_fields():
    q = (QUERIES / "tokens-by-principal.txt").read_text()
    for field in ("identity.arn", "input.inputTokenCount", "output.outputTokenCount"):
        assert field in q


def test_lab_query_takes_label_from_config(cfg):
    cfg["traffic"]["metadataLab"] = "other-lab"
    assert 'requestMetadata.lab = "other-lab"' in verify.lab_query(cfg)


# ---------------------------------------------------------------- Cost Explorer requests match the API model

def test_ce_requests_match_botocore_model(cfg):
    shape = input_shape("ce", "GetCostAndUsage")
    svc = verify.service_filter(["Amazon Bedrock"])
    validate_parameters(verify.ce_request(date(2026, 10, 20), date(2026, 10, 21), svc, "team"), shape)
    both = {"And": [svc, verify.tag_filter("path", ["gateway"])]}
    validate_parameters(verify.ce_request(date(2026, 10, 20), date(2026, 10, 21), both, "team"), shape)
    validate_parameters(verify.ce_request(date(2026, 10, 20), date(2026, 10, 21), svc, None), shape)


def test_tag_status_update_matches_botocore_model():
    shape = input_shape("ce", "UpdateCostAllocationTagsStatus")
    validate_parameters({"CostAllocationTagsStatus": [{"TagKey": "team", "Status": "Active"}]}, shape)


def test_tag_status_batches_of_20():
    sent = []

    class CE:
        def update_cost_allocation_tags_status(self, CostAllocationTagsStatus):
            sent.append(len(CostAllocationTagsStatus))
            return {"Errors": []}

    activate_tags.set_status(CE(), [f"k{i}" for i in range(45)], "Active")
    assert sent == [20, 20, 5]


def test_tag_key_matching_accepts_prefixed_keys(cfg):
    listed = [{"TagKey": "iamPrincipal/team"}, {"TagKey": "team"}, {"TagKey": "teamster"}, {"TagKey": "app"}]
    selected = activate_tags.select_keys(listed, cfg["tagKeys"])
    assert selected["team"] == ["iamPrincipal/team", "team"]
    assert selected["app"] == ["app"]
    assert selected["user"] == []


# ---------------------------------------------------------------- CUR parsing

@pytest.mark.parametrize("raw", [
    {"iamPrincipal/team": "search", "resourceTags/project": "x"},
    [("iamPrincipal/team", "search"), ("resourceTags/project", "x")],
    '{"iamPrincipal/team": "search", "resourceTags/project": "x"}',
])
def test_cur_tags_column_forms(raw):
    tags = verify.tags_dict(raw)
    assert verify.principal_tag(tags, "team") == "search"
    assert verify.principal_tag(tags, "user") is None


def test_cur_tags_empty():
    assert verify.tags_dict(None) == {}


# ---------------------------------------------------------------- export fallback matches the stack

def test_export_cli_matches_cdk_columns_and_api_model(cfg):
    cols = create_export_cli.cur_columns()
    assert "line_item_iam_principal" in cols and "tags" in cols
    stack_src = (REPO_ROOT / "cdk" / "lib" / "cur-export-stack.ts").read_text()
    table_cfg = create_export_cli.export_definition(cfg, "b")["DataQuery"]["TableConfigurations"]
    for key in re.findall(r"(INCLUDE_[A-Z_]+): '", stack_src):
        assert key in table_cfg["COST_AND_USAGE_REPORT"]
    validate_parameters(
        {"Export": create_export_cli.export_definition(cfg, "example-bucket"),
         "ResourceTags": [{"Key": "project", "Value": "mt-01-bedrock-cost-lab"}]},
        input_shape("bcm-data-exports", "CreateExport"),
    )


# ---------------------------------------------------------------- one-day windows (deploy log entry 13)

DAY0 = date(2026, 10, 21)
TODAY = date(2026, 10, 23)


class FakeCE:
    """Records Cost Explorer requests; returns a service name and an empty day (as seen for day 0)."""

    def __init__(self, services=("Amazon Bedrock",)):
        self.services, self.dimension_calls, self.cost_calls = list(services), [], []

    def get_dimension_values(self, **kw):
        self.dimension_calls.append(kw)
        return {"DimensionValues": [{"Value": s} for s in self.services]}

    def get_cost_and_usage(self, **kw):
        self.cost_calls.append(kw)
        start = kw["TimePeriod"]["Start"]
        return {"ResultsByTime": [{"TimePeriod": {"Start": start}, "Groups": [], "Total": {}}]}


class FakeLogs:
    def __init__(self):
        self.started = []

    def start_query(self, **kw):
        self.started.append(kw)
        return {"queryId": "q"}

    def get_query_results(self, queryId):
        return {"status": "Complete", "results": []}


class FakeSession:
    def __init__(self, ce, logs=None):
        self.clients = {"ce": ce, "logs": logs}

    def client(self, name):
        return self.clients[name]


@pytest.fixture
def offline(monkeypatch):
    import pandas as pd

    monkeypatch.setattr(verify, "utc_now", lambda: verify.datetime(2026, 10, 23, 14, 30, tzinfo=verify.UTC))
    monkeypatch.setattr(verify, "first_day_of_mode", lambda mode=None: DAY0)
    monkeypatch.setattr(verify, "last_day_of_mode", lambda mode: DAY0)
    monkeypatch.setattr(verify, "activation", lambda: None)
    monkeypatch.setattr(verify, "lab_outputs", lambda sess, cfg: {"CurBucketName": "b", "ProfileArn": "p"})
    cur = pd.DataFrame({
        verify.COL_USAGE_START: pd.to_datetime(["2026-10-21T15:00:00Z"], utc=True),
        verify.COL_COST: [0.01], verify.COL_TAGS: [{"iamPrincipal/team": "search"}],
        verify.COL_PRINCIPAL: ["arn:aws:sts::ACCOUNT:assumed-role/x/y"], verify.COL_RESOURCE: ["r"],
    })
    monkeypatch.setattr(verify, "load_cur", lambda sess, cfg, outputs: cur)


class Args:
    day = None


def test_day_window_is_start_inclusive_end_exclusive():
    assert verify.day_window(DAY0) == (date(2026, 10, 21), date(2026, 10, 22))
    req = verify.ce_request(*verify.day_window(DAY0), verify.service_filter(["Amazon Bedrock"]), None)
    assert req["TimePeriod"] == {"Start": "2026-10-21", "End": "2026-10-22"}


def test_logs_window_covers_the_whole_utc_day():
    start, end = verify.logs_window(DAY0)
    assert verify.datetime.fromtimestamp(start, verify.UTC).isoformat() == "2026-10-21T00:00:00+00:00"
    assert verify.datetime.fromtimestamp(end, verify.UTC).isoformat() == "2026-10-21T23:59:59+00:00"


@pytest.mark.parametrize("test_name", ["t2", "t4", "t5"])
def test_single_day_subcommands_send_one_day_windows(cfg, offline, test_name):
    cfg["pricing"].update(inputPer1M=0.07, outputPer1M=0.30)  # t4 refuses to run without prices
    ce, logs = FakeCE(), FakeLogs()
    getattr(verify, test_name)(cfg, FakeSession(ce, logs), Args())
    # The cost query reads exactly the pre-activation day...
    assert [c["TimePeriod"] for c in ce.cost_calls] == [{"Start": "2026-10-21", "End": "2026-10-22"}]
    # ...while the service name is looked up from the first lab call through tomorrow.
    assert ce.dimension_calls[0]["TimePeriod"] == {"Start": "2026-10-21", "End": "2026-10-24"}
    if test_name == "t4":
        assert (logs.started[0]["startTime"], logs.started[0]["endTime"]) == verify.logs_window(DAY0)


@pytest.mark.parametrize("test_name", ["t1", "t3"])
def test_range_subcommands_read_through_today(cfg, offline, test_name):
    ce = FakeCE()
    getattr(verify, test_name)(cfg, FakeSession(ce), Args())
    assert ce.cost_calls[0]["TimePeriod"] == {"Start": "2026-10-21", "End": "2026-10-24"}


def test_t5_keeps_going_when_cost_explorer_has_no_bedrock_service(cfg, offline):
    ce = FakeCE(services=())
    record = verify.t5(cfg, FakeSession(ce), Args())
    assert record["costExplorer"] == [] and record["costExplorerNote"] == verify.NO_SERVICE_NOTE
    assert ce.cost_calls == []
    assert record["curHourly"][0]["team"] == "search"  # the CUR half still ran


def test_empty_grouped_day_is_reported_as_no_data_not_zero(cfg, offline):
    record = verify.t5(cfg, FakeSession(FakeCE()), Args())
    assert record["costExplorer"] == [{"date": "2026-10-21", "group": verify.NO_DATA, "amount": 0.0, "unit": ""}]


# ---------------------------------------------------------------- t5 activation hour (deploy log entry 17)

ACT = verify.datetime(2026, 10, 22, 12, 17, 44, tzinfo=verify.UTC)


def _hour(h):
    return verify.datetime(2026, 10, 22, h, tzinfo=verify.UTC)


def test_activation_label_inside_an_hour():
    assert verify.activation_label(_hour(11), ACT) == "before"
    assert verify.activation_label(_hour(12), ACT) == verify.ACTIVATION_HOUR  # 12:00-13:00 holds 12:17:44
    assert verify.activation_label(_hour(13), ACT) == "after"


def test_activation_label_on_the_hour_boundary():
    on_the_hour = _hour(12)
    assert verify.activation_label(_hour(11), on_the_hour) == "before"
    assert verify.activation_label(_hour(12), on_the_hour) == "after"
    assert verify.activation_label(_hour(12), None) == "unknown"


def test_t5_labels_the_activation_hour_and_says_it_mixes(cfg, offline, monkeypatch, capsys):
    import pandas as pd

    monkeypatch.setattr(verify, "activation", lambda: {"keys": {"iamPrincipal/team": {"activatedAt": utc(ACT)}}})
    starts = ["2026-10-22T11:00:00Z", "2026-10-22T12:00:00Z", "2026-10-22T13:00:00Z"]
    cur = pd.DataFrame({
        verify.COL_USAGE_START: pd.to_datetime(starts, utc=True), verify.COL_COST: [0.01, 0.02, 0.03],
        verify.COL_TAGS: [{}, {"iamPrincipal/team": "search"}, {"iamPrincipal/team": "search"}],
        verify.COL_PRINCIPAL: ["p"] * 3, verify.COL_RESOURCE: ["r"] * 3,
    })
    monkeypatch.setattr(verify, "load_cur", lambda sess, cfg, outputs: cur)
    record = verify.t5(cfg, FakeSession(FakeCE()), Args())
    assert [(r["hour"], r["vsActivation"]) for r in record["curHourly"]] == [
        ("2026-10-22T11:00Z", "before"),
        ("2026-10-22T12:00Z", verify.ACTIVATION_HOUR),
        ("2026-10-22T13:00Z", "after"),
    ]
    assert "mixes calls made before and after activation" in capsys.readouterr().out


def utc(dt):
    return dt.isoformat().replace("+00:00", "Z")
