"""Day 1: activate the lab's cost allocation tag keys (spec section 6.3). Writes runs/activation.json."""

from __future__ import annotations

import argparse
import sys

import _bootstrap  # noqa: F401

from traffic.common import RUNS_DIR, read_json, session, utc_iso, write_json
from traffic.config import load_config
from traffic.redact import err, set_show_ids, table

BATCH = 20
NOT_LISTED_HELP = (
    "Tags appear in Billing only after the principal has made at least one Bedrock call, and can take up to "
    "24 hours — retry later, or activate in the console: Billing and Cost Management → Cost allocation tags → "
    "filter type IAM principal → Activate"
)


def list_all_tags(ce) -> list[dict]:
    tags, token = [], None
    while True:
        kwargs = {"NextToken": token} if token else {}
        resp = ce.list_cost_allocation_tags(**kwargs)
        tags.extend(resp.get("CostAllocationTags", []))
        token = resp.get("NextToken")
        if not token:
            return tags


def matches(listed_key: str, configured_key: str) -> bool:
    """IAM-principal tags may be listed with a prefix (e.g. 'iamPrincipal/team')."""
    return listed_key == configured_key or listed_key.endswith(f"/{configured_key}")


def select_keys(listed: list[dict], configured: dict[str, str]) -> dict[str, list[str]]:
    return {name: sorted({t["TagKey"] for t in listed if matches(t["TagKey"], key)})
            for name, key in configured.items()}


def set_status(ce, keys: list[str], status: str) -> list[dict]:
    errors = []
    for i in range(0, len(keys), BATCH):
        entries = [{"TagKey": k, "Status": status} for k in keys[i:i + BATCH]]
        resp = ce.update_cost_allocation_tags_status(CostAllocationTagsStatus=entries)
        errors.extend(resp.get("Errors", []))
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--config")
    parser.add_argument("--show-ids", action="store_true")
    args = parser.parse_args(argv)
    set_show_ids(args.show_ids)
    cfg = load_config(args.config)
    ce = session(cfg, args.profile).client("ce")

    listed = list_all_tags(ce)
    table([{"key": t["TagKey"], "type": t.get("Type"), "status": t.get("Status"),
            "lastUsed": t.get("LastUsedDate", "")} for t in listed], ["key", "type", "status", "lastUsed"])

    selected = select_keys(listed, cfg["tagKeys"])
    to_activate = sorted({k for keys in selected.values() for k in keys})
    errors = set_status(ce, to_activate, "Active") if to_activate else []
    failed = {e.get("TagKey") for e in errors}
    for e in errors:
        err(f"{e.get('TagKey')}: {e.get('Code')} {e.get('Message')}")

    now = utc_iso()
    previous = (read_json(RUNS_DIR / "activation.json") or {}).get("keys", {})
    keys_record = dict(previous)
    for name, listed_keys in selected.items():
        for k in listed_keys:
            if k not in failed and k not in keys_record:
                keys_record[k] = {"configuredKey": cfg["tagKeys"][name], "activatedAt": now,
                                  "type": next((t.get("Type") for t in listed if t["TagKey"] == k), None)}

    missing = [cfg["tagKeys"][name] for name, keys in selected.items() if not keys]
    write_json(RUNS_DIR / "activation.json", {
        "keys": keys_record,
        "listedAs": {cfg["tagKeys"][name]: keys for name, keys in selected.items()},
        "notListedYet": missing,
        "errors": errors,
    })
    table([{"key": k, **v} for k, v in keys_record.items()], ["key", "configuredKey", "type", "activatedAt"])
    if missing:
        err(f"Not listed yet: {', '.join(missing)}. {NOT_LISTED_HELP}")
        return 2
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
