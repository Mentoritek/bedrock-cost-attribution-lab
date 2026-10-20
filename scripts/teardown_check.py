"""After both `cdk destroy` runs: report anything the lab left behind (spec section 6.6)."""

from __future__ import annotations

import argparse
import sys

import _bootstrap  # noqa: F401
from activate_tags import set_status

from traffic.common import RESULTS_DIR, RUNS_DIR, error_code, read_json, session, write_json
from traffic.config import load_config
from traffic.redact import err, out, set_show_ids, table


def check(cfg, sess) -> list[dict]:
    rows = []

    def add(item: str, remains: bool, detail: str = "") -> None:
        rows.append({"item": item, "status": "REMAINS" if remains else "gone", "detail": detail})

    logging_cfg = sess.client("bedrock").get_model_invocation_logging_configuration().get("loggingConfig")
    add("Bedrock invocation logging configuration", bool(logging_cfg),
        (logging_cfg or {}).get("cloudWatchConfig", {}).get("logGroupName", ""))

    exports = []
    for page in sess.client("bcm-data-exports").get_paginator("list_exports").paginate():
        exports += [e for e in page.get("Exports", []) if e.get("ExportName") == cfg["curExport"]["name"]]
    add(f"CUR 2.0 export {cfg['curExport']['name']}", bool(exports))

    s3 = sess.client("s3")
    saved = (read_json(RESULTS_DIR / "stack-outputs.json") or {}).get("outputs", {})
    candidates = {saved["CurBucketName"]} if saved.get("CurBucketName") else set()
    prefix = cfg["stackNames"]["lab"].lower()
    candidates |= {b["Name"] for b in s3.list_buckets().get("Buckets", []) if b["Name"].startswith(prefix)}
    remaining = []
    for name in sorted(candidates):
        try:
            s3.head_bucket(Bucket=name)
            remaining.append(name)
        except Exception as exc:
            if error_code(exc) not in ("404", "NoSuchBucket", "NotFound"):
                remaining.append(f"{name} ({error_code(exc)})")
    add("CUR bucket from the lab stack", bool(remaining), ", ".join(remaining))

    profiles = []
    for page in sess.client("bedrock").get_paginator("list_inference_profiles").paginate(typeEquals="APPLICATION"):
        profiles += [p for p in page.get("inferenceProfileSummaries", [])
                     if p.get("inferenceProfileName") == cfg["profile"]["name"]]
    add(f"Application inference profile {cfg['profile']['name']}", bool(profiles))

    groups = sess.client("logs").describe_log_groups(logGroupNamePrefix=cfg["logGroupName"]).get("logGroups", [])
    add(f"Log group {cfg['logGroupName']}", any(g["logGroupName"] == cfg["logGroupName"] for g in groups))

    account = sess.client("sts").get_caller_identity()["Account"]
    try:
        sess.client("budgets").describe_budget(AccountId=account, BudgetName=cfg["budget"]["name"])
        add(f"Budget {cfg['budget']['name']}", True)
    except Exception as exc:
        add(f"Budget {cfg['budget']['name']}", error_code(exc) != "NotFoundException", error_code(exc))

    cfn = sess.client("cloudformation")
    for name in list(cfg["stackNames"].values()) + ["CDKToolkit"]:
        try:
            status = cfn.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"]
            add(f"Stack {name}", status != "DELETE_COMPLETE", status +
                (" (CDK bootstrap — kept unless you delete it; see README Teardown)" if name == "CDKToolkit" else ""))
        except Exception as exc:
            add(f"Stack {name}", False, error_code(exc))
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--config")
    parser.add_argument("--show-ids", action="store_true")
    parser.add_argument("--deactivate-tags", action="store_true",
                        help="set the lab's activated cost allocation tag keys back to Inactive")
    args = parser.parse_args(argv)
    set_show_ids(args.show_ids)
    cfg = load_config(args.config)
    sess = session(cfg, args.profile)

    rows = check(cfg, sess)
    record: dict = {"items": rows}
    if args.deactivate_tags:
        act = read_json(RUNS_DIR / "activation.json") or {}
        keys = sorted(act.get("keys", {})) or sorted(cfg["tagKeys"].values())
        errors = set_status(sess.client("ce"), keys, "Inactive")
        record["deactivated"] = {"keys": keys, "errors": errors}
        for e in errors:
            err(f"{e.get('TagKey')}: {e.get('Code')} {e.get('Message')}")
        out(f"Deactivated tag keys: {', '.join(keys)}")
    table(rows, ["item", "status", "detail"])
    write_json(RESULTS_DIR / "teardown.json", record)
    leftovers = [r for r in rows if r["status"] == "REMAINS" and not r["item"].endswith("CDKToolkit")]
    out("Nothing left from the lab except the CDK bootstrap stack." if not leftovers
        else f"{len(leftovers)} lab item(s) remain — see above.")
    return 1 if leftovers else 0


if __name__ == "__main__":
    sys.exit(main())
