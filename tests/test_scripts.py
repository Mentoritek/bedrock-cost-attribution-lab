"""Offline tests for scripts/ helpers and query files. No AWS calls."""

import re
from datetime import date

import activate_tags
import create_export_cli
import pytest
import verify
from botocore.session import get_session
from botocore.validate import validate_parameters

from traffic.config import REPO_ROOT, load_config

QUERIES = REPO_ROOT / "queries" / "logs-insights"


@pytest.fixture
def cfg():
    return load_config()


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
