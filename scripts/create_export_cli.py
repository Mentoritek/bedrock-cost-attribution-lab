"""Fallback for the CUR 2.0 export if CloudFormation rejects CurExportStack.

CloudFormation documents TableConfigurations as "Object of String", while the Data Exports API
takes a nested map keyed by table name. The stack uses the nested map. If `cdk deploy
BedrockCostLabCurExport` fails on that property, create the same export with the API instead:

    python scripts/create_export_cli.py create
    python scripts/create_export_cli.py delete     # at teardown, instead of cdk destroy BedrockCostLabCurExport

The export definition (columns, table configuration, destination) matches cdk/lib/cur-export-stack.ts.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

import _bootstrap  # noqa: F401

from traffic.common import RESULTS_DIR, lab_outputs, session, write_json
from traffic.config import REPO_ROOT, load_config
from traffic.redact import out, set_show_ids

STACK_SOURCE = REPO_ROOT / "cdk" / "lib" / "cur-export-stack.ts"
TABLE = "COST_AND_USAGE_REPORT"


def cur_columns() -> list[str]:
    """Reads CUR_COLUMNS from the CDK stack so both paths export exactly the same columns."""
    src = STACK_SOURCE.read_text(encoding="utf-8")
    block = re.search(r"CUR_COLUMNS = \[(.*?)\];", src, re.S)
    if not block:
        raise SystemExit(f"could not find CUR_COLUMNS in {STACK_SOURCE}")
    return re.findall(r"'([a-z_]+)'", block.group(1))


def export_definition(cfg: dict, bucket: str) -> dict:
    return {
        "Name": cfg["curExport"]["name"],
        "DataQuery": {
            "QueryStatement": f"SELECT {', '.join(cur_columns())} FROM {TABLE}",
            "TableConfigurations": {
                TABLE: {
                    "TIME_GRANULARITY": cfg["curExport"]["timeGranularity"],
                    "INCLUDE_RESOURCES": "TRUE",
                    "INCLUDE_IAM_PRINCIPAL_DATA": "TRUE",
                    "INCLUDE_SPLIT_COST_ALLOCATION_DATA": "FALSE",
                    "INCLUDE_CAPACITY_RESERVATION_DATA": "FALSE",
                    "INCLUDE_MANUAL_DISCOUNT_COMPATIBILITY": "FALSE",
                }
            },
        },
        "DestinationConfigurations": {
            "S3Destination": {
                "S3Bucket": bucket,
                "S3Prefix": cfg["curExport"]["s3Prefix"],
                "S3Region": cfg["region"],
                "S3OutputConfigurations": {
                    "OutputType": "CUSTOM",
                    "Format": "PARQUET",
                    "Compression": "PARQUET",
                    "Overwrite": "OVERWRITE_REPORT",
                },
            }
        },
        "RefreshCadence": {"Frequency": "SYNCHRONOUS"},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["create", "delete", "show"])
    parser.add_argument("--profile")
    parser.add_argument("--config")
    parser.add_argument("--show-ids", action="store_true")
    args = parser.parse_args(argv)
    set_show_ids(args.show_ids)
    cfg = load_config(args.config)

    if args.action == "show":
        out(json.dumps(export_definition(cfg, "<CurBucketName>"), indent=2))
        return 0

    sess = session(cfg, args.profile)
    client = sess.client("bcm-data-exports")
    if args.action == "create":
        bucket = lab_outputs(sess, cfg)["CurBucketName"]
        arn = client.create_export(
            Export=export_definition(cfg, bucket),
            ResourceTags=[{"Key": cfg["projectTag"]["key"], "Value": cfg["projectTag"]["value"]}],
        )["ExportArn"]
        write_json(RESULTS_DIR / "export-cli.json", {"exportArn": arn, "createdVia": "create_export_cli.py"})
        out(f"Created export {cfg['curExport']['name']}: {arn}")
        return 0

    for page in client.get_paginator("list_exports").paginate():
        for e in page.get("Exports", []):
            if e.get("ExportName") == cfg["curExport"]["name"]:
                client.delete_export(ExportArn=e["ExportArn"])
                out(f"Deleted export {e['ExportName']}")
                return 0
    out(f"No export named {cfg['curExport']['name']} found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
