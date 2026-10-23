"""Reads what billing and logs show for each test and records it (spec section 6.4).

    python scripts/verify.py t0|t1|t2|t3|t4|t5|t6 [--day YYYY-MM-DD]

Each subcommand prints a table and writes results/<test>.json. It reports values; it never
decides a verdict. Cost Explorer API requests may be billed — run these a few times, not in a loop.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401

from traffic.common import (
    RESULTS_DIR,
    RUNS_DIR,
    error_code,
    lab_outputs,
    read_json,
    session,
    utc_iso,
    utc_now,
    write_json,
)
from traffic.config import REPO_ROOT, load_config
from traffic.redact import out, set_show_ids, table

QUERIES_DIR = REPO_ROOT / "queries" / "logs-insights"
CUR_CACHE = RESULTS_DIR / "cur-cache"
CE_NOTE = "Note: Cost Explorer API requests may be billed (README 'Cost') — run this a few times, not in a loop."

# CUR 2.0 column names (CUR 2.0 table dictionary).
COL_USAGE_START = "line_item_usage_start_date"
COL_PRODUCT = "line_item_product_code"
COL_RESOURCE = "line_item_resource_id"
COL_COST = "line_item_unblended_cost"
COL_PRINCIPAL = "line_item_iam_principal"
COL_TAGS = "tags"
IAM_PRINCIPAL_PREFIX = "iamPrincipal/"


# ---------------------------------------------------------------- local records

def run_records() -> list[dict[str, Any]]:
    records = []
    for path in sorted(RUNS_DIR.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                rec["_file"] = path.name
                records.append(rec)
    return records


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def activation() -> dict[str, Any] | None:
    return read_json(RUNS_DIR / "activation.json")


def activation_time(act: dict[str, Any] | None) -> datetime | None:
    if not act or not act.get("keys"):
        return None
    return min(parse_ts(v["activatedAt"]) for v in act["keys"].values())


def listed_key(cfg: dict[str, Any], name: str, act: dict[str, Any] | None) -> str:
    """The tag key as Billing lists it (activate_tags.py records it); falls back to the configured key."""
    configured = cfg["tagKeys"][name]
    listed = ((act or {}).get("listedAs") or {}).get(configured) or []
    return listed[0] if listed else configured


def first_day_of_mode(mode: str | None = None) -> date | None:
    stamps = [parse_ts(r["timestamp"]) for r in run_records() if mode is None or r.get("mode") == mode]
    return min(stamps).date() if stamps else None


def last_day_of_mode(mode: str) -> date | None:
    stamps = [parse_ts(r["timestamp"]) for r in run_records() if r.get("mode") == mode]
    return max(stamps).date() if stamps else None


def pick_day(arg: str | None, fallback: date | None) -> date:
    if arg:
        return date.fromisoformat(arg)
    return fallback or utc_now().date()


# ---------------------------------------------------------------- Cost Explorer

def service_filter(values: list[str]) -> dict[str, Any]:
    return {"Dimensions": {"Key": "SERVICE", "Values": values}}


def ce_request(start: date, end: date, flt: dict[str, Any], group_key: str | None) -> dict[str, Any]:
    req: dict[str, Any] = {
        "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},  # End is exclusive
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
        "Filter": flt,
    }
    if group_key:
        req["GroupBy"] = [{"Type": "TAG", "Key": group_key}]
    return req


def tag_filter(key: str, values: list[str]) -> dict[str, Any]:
    return {"Tags": {"Key": key, "Values": values, "MatchOptions": ["EQUALS"]}}


def day_window(day: date) -> tuple[date, date]:
    """One UTC day for Cost Explorer: Start is inclusive, End is exclusive (the next day)."""
    return day, day + timedelta(days=1)


def logs_window(day: date) -> tuple[int, int]:
    """One UTC day for Logs Insights, in epoch seconds: 00:00:00 to 23:59:59 (both ends inclusive)."""
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp()) - 1


def discovery_window(start: date) -> tuple[date, date]:
    """Where to look up the Bedrock SERVICE name: from the lab's first call (or start, if earlier) to tomorrow.

    The name doesn't depend on the day being read, so a day that Cost Explorer returns no data for
    (seen for the account's first day in our run, while CUR had that day's cost) must not hide it.
    """
    first = first_day_of_mode() or start
    return min(first, start), utc_now().date() + timedelta(days=1)


def bedrock_services(ce, start: date, end: date) -> list[str]:
    """Discovers the Bedrock SERVICE dimension value(s) instead of hard-coding them. May be empty."""
    resp = ce.get_dimension_values(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Dimension="SERVICE",
        SearchString="Bedrock",
    )
    return [v["Value"] for v in resp.get("DimensionValues", [])]


NO_SERVICE_NOTE = ("Cost Explorer lists no SERVICE value matching 'Bedrock' since the first lab call "
                   "(cost data can take up to 24 hours) — Cost Explorer part skipped; CUR part still runs.")


def bedrock_cost(ce, start: date, end: date, group_key: str | None,
                 extra_filter: dict[str, Any] | None = None) -> tuple[list[str], list[dict[str, Any]], str | None]:
    """Bedrock cost from Start (inclusive) to End (exclusive). Returns (services, rows, note)."""
    services = bedrock_services(ce, *discovery_window(start))
    if not services:
        out(NO_SERVICE_NOTE)
        return [], [], NO_SERVICE_NOTE
    flt = service_filter(services) if extra_filter is None else {"And": [service_filter(services), extra_filter]}
    return services, ce_rows(ce, ce_request(start, end, flt, group_key)), None


NO_DATA = "(no data returned)"


def ce_rows(ce, req: dict[str, Any]) -> list[dict[str, Any]]:
    rows, token = [], None
    while True:
        resp = ce.get_cost_and_usage(**req, **({"NextPageToken": token} if token else {}))
        for period in resp.get("ResultsByTime", []):
            day = period["TimePeriod"]["Start"]
            if period.get("Groups"):
                for g in period["Groups"]:
                    m = g["Metrics"]["UnblendedCost"]
                    rows.append({"date": day, "group": "|".join(g["Keys"]), "amount": float(m["Amount"]),
                                 "unit": m["Unit"]})
            elif "GroupBy" in req:
                # A grouped query with no groups means Cost Explorer returned nothing for that day,
                # which is not the same as a cost of $0 — say so instead of printing a total.
                rows.append({"date": day, "group": NO_DATA, "amount": 0.0, "unit": ""})
            else:
                m = period["Total"].get("UnblendedCost", {"Amount": "0", "Unit": "USD"})
                rows.append({"date": day, "group": "(total)", "amount": float(m["Amount"]), "unit": m["Unit"]})
        token = resp.get("NextPageToken")
        if not token:
            return rows


def money(x: float) -> str:
    return f"{x:.6f}"


# ---------------------------------------------------------------- CUR 2.0

def load_cur(sess, cfg: dict[str, Any], outputs: dict[str, Any]):
    """Downloads the export's Parquet files under <s3Prefix>/ and returns Bedrock rows as a DataFrame."""
    import pandas as pd

    s3 = sess.client("s3")
    bucket = outputs["CurBucketName"]
    prefix = cfg["curExport"]["s3Prefix"].rstrip("/") + "/"
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys += [o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(".parquet")]
    if not keys:
        raise SystemExit(f"No Parquet files under s3://<CurBucketName>/{prefix} yet — the export refreshes up to "
                         "three times a day after it is created.")
    CUR_CACHE.mkdir(parents=True, exist_ok=True)
    frames = []
    for key in keys:
        local = CUR_CACHE / key.replace("/", "__")
        s3.download_file(bucket, key, str(local))
        frames.append(pd.read_parquet(local))
    df = pd.concat(frames, ignore_index=True)
    out(f"CUR: {len(keys)} file(s), {len(df)} rows; product codes: {sorted(df[COL_PRODUCT].dropna().unique())}")
    bedrock = df[df[COL_PRODUCT].fillna("").str.contains("bedrock", case=False)].copy()
    bedrock[COL_USAGE_START] = pd.to_datetime(bedrock[COL_USAGE_START], utc=True)
    return bedrock


def tags_dict(value: Any) -> dict[str, str]:
    """The CUR 2.0 `tags` column is map<string,string>; pyarrow may hand it over as a dict, pairs, or JSON."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, str):
        try:
            return tags_dict(json.loads(value))
        except ValueError:
            return {}
    try:
        return {str(k): str(v) for k, v in list(value)}
    except (TypeError, ValueError):
        return {}


def principal_tag(tags: dict[str, str], key: str) -> str | None:
    """Value of an IAM-principal tag: 'iamPrincipal/<key>' (documented prefix), else any '<prefix>/<key>'."""
    if f"{IAM_PRINCIPAL_PREFIX}{key}" in tags:
        return tags[f"{IAM_PRINCIPAL_PREFIX}{key}"]
    for k, v in tags.items():
        if k.endswith(f"/{key}"):
            return v
    return None


def cur_group(df, cfg: dict[str, Any], by_principal: bool = True) -> list[dict[str, Any]]:
    keys = cfg["tagKeys"]
    work = df.copy()
    parsed = work[COL_TAGS].map(tags_dict)
    for name in ("team", "path", "user"):
        work[name] = parsed.map(lambda t, k=keys[name]: principal_tag(t, k) or "(none)")
    cols = ([COL_PRINCIPAL] if by_principal else []) + ["team", "path", "user"]
    work[COL_PRINCIPAL] = work[COL_PRINCIPAL].fillna("(null)")
    grouped = work.groupby(cols, dropna=False)[COL_COST].agg(["sum", "count"]).reset_index()
    return [{**{c: r[c] for c in cols}, "cost": money(r["sum"]), "rows": int(r["count"])}
            for _, r in grouped.sort_values("sum", ascending=False).iterrows()]


# ---------------------------------------------------------------- Logs Insights

def lab_query(cfg: dict[str, Any]) -> str:
    """The pasteable query file, with its requestMetadata.lab filter set to the configured label."""
    text = (QUERIES_DIR / "tokens-by-team-feature.txt").read_text(encoding="utf-8")
    label = cfg["traffic"]["metadataLab"]
    return re.sub(r'(requestMetadata\.lab = )"[^"]*"', lambda m: f'{m.group(1)}"{label}"', text)


# ---------------------------------------------------------------- tests

def t0(cfg, sess, args) -> dict[str, Any]:
    pre = read_json(RESULTS_DIR / "preflight.json") or {}
    cfn = sess.client("cloudformation")
    stacks = {}
    for name in cfg["stackNames"].values():
        try:
            stacks[name] = cfn.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"]
        except Exception as exc:
            missing = error_code(exc) == "ValidationError"  # DescribeStacks on a stack that doesn't exist
            if missing and name == cfg["stackNames"]["curExport"]:
                stacks[name] = "not deployed yet (deployed on day 1)"
            else:
                stacks[name] = f"not found ({error_code(exc)})"
    logging_cfg = sess.client("bedrock").get_model_invocation_logging_configuration().get("loggingConfig")
    smoke = read_json(RESULTS_DIR / "smoke.json")
    if not smoke:
        raise SystemExit("results/smoke.json not found — run `python -m traffic smoke` first.")

    logs = sess.client("logs")
    found = []
    for call in smoke["calls"]:
        rid = call.get("requestId")
        hit = False
        if rid and not call.get("error"):
            t = parse_ts(call["timestamp"])
            resp = logs.filter_log_events(
                logGroupName=cfg["logGroupName"],
                startTime=int((t - timedelta(minutes=1)).timestamp() * 1000),
                endTime=int((t + timedelta(minutes=15)).timestamp() * 1000),
                filterPattern=f'"{rid}"',
            )
            hit = bool(resp.get("events"))
        found.append({"identity": call.get("identity"), "requestId": rid or "",
                      "smoke": "OK" if not call.get("error") else "FAIL", "logEventFound": hit})

    out(f"Account type (preflight): {pre.get('accountType', 'run scripts/preflight.py')}")
    table([{"stack": k, "status": v} for k, v in stacks.items()])
    out(f"Invocation logging configured: {bool(logging_cfg)}"
        f" → {(logging_cfg or {}).get('cloudWatchConfig', {}).get('logGroupName', '-')}")
    table(found, ["identity", "smoke", "requestId", "logEventFound"])
    return {"accountType": pre.get("accountType"), "stacks": stacks, "loggingConfig": logging_cfg,
            "smokeRunId": smoke.get("runId"), "smokeLogs": found}


def t1(cfg, sess, args) -> dict[str, Any]:
    ce = sess.client("ce")
    act = activation()
    act_at = activation_time(act)
    start = pick_day(args.day, first_day_of_mode())
    end = utc_now().date() + timedelta(days=1)
    team_key = listed_key(cfg, "team", act)
    services, rows, note = bedrock_cost(ce, start, end, team_key)
    first_seen: dict[str, str] = {}
    for r in rows:
        if r["amount"] > 0 and r["group"] not in first_seen:
            first_seen[r["group"]] = r["date"]
    now = utc_now()
    hours = round((now - act_at).total_seconds() / 3600, 1) if act_at else None
    out(CE_NOTE)
    act_text = utc_iso(act_at) if act_at else "not recorded"
    out(f"Grouped by tag key {team_key!r}; services {services}; activation {act_text}")
    table([{"group": g, "firstDateWithCost": d} for g, d in sorted(first_seen.items())])
    table([{**r, "amount": money(r["amount"])} for r in rows if r["amount"] > 0], ["date", "group", "amount", "unit"])
    out(f"Hours since activation at this check: {hours}")
    return {"teamKey": team_key, "services": services, "activatedAt": utc_iso(act_at) if act_at else None,
            "checkedAt": utc_iso(now), "hoursSinceActivation": hours, "firstSeen": first_seen, "rows": rows,
            "costExplorerNote": note}


def t2(cfg, sess, args) -> dict[str, Any]:
    ce = sess.client("ce")
    act = activation()
    day = pick_day(args.day, first_day_of_mode("gateway-shared"))
    team_key, path_key = listed_key(cfg, "team", act), listed_key(cfg, "path", act)
    path_filter = tag_filter(path_key, [cfg["gateway"]["roleTag"]["path"]])
    _, ce_result, note = bedrock_cost(ce, *day_window(day), team_key, path_filter)
    out(CE_NOTE)
    out(f"Cost Explorer {day}: grouped by {team_key!r}, filtered {path_key}={cfg['gateway']['roleTag']['path']!r}")
    table([{**r, "amount": money(r["amount"])} for r in ce_result], ["date", "group", "amount", "unit"])

    outputs = lab_outputs(sess, cfg)
    df = load_cur(sess, cfg, outputs)
    day_rows = df[df[COL_USAGE_START].dt.date == day]
    cur_rows = cur_group(day_rows, cfg)
    out(f"CUR 2.0 Bedrock rows on {day}, by {COL_PRINCIPAL} and IAM-principal tags:")
    table(cur_rows)
    return {"day": day.isoformat(), "teamKey": team_key, "pathKey": path_key, "costExplorer": ce_result,
            "costExplorerNote": note, "cur": cur_rows}


def t3(cfg, sess, args) -> dict[str, Any]:
    ce = sess.client("ce")
    act = activation()
    start = pick_day(args.day, first_day_of_mode())
    end = utc_now().date() + timedelta(days=1)
    app_key = listed_key(cfg, "app", act)
    _, ce_result, note = bedrock_cost(ce, start, end, app_key)
    out(CE_NOTE)
    out(f"Cost Explorer grouped by {app_key!r}:")
    table([{**r, "amount": money(r["amount"])} for r in ce_result if r["amount"] > 0],
          ["date", "group", "amount", "unit"])

    outputs = lab_outputs(sess, cfg)
    df = load_cur(sess, cfg, outputs)
    profile_rows = df[df[COL_RESOURCE] == outputs["ProfileArn"]]
    by_day = profile_rows.groupby(profile_rows[COL_USAGE_START].dt.date)[COL_COST].sum()
    cur_rows = [{"date": str(d), "resourceId": outputs["ProfileArn"], "cost": money(c)} for d, c in by_day.items()]
    out("CUR 2.0 rows whose resource ID is the application inference profile:")
    table(cur_rows)

    cc = read_json(RESULTS_DIR / "t3-chat-completions.json") or {}
    statuses = [{"request": k, "model": v["model"], "status": v["status"]}
                for k, v in (cc.get("results") or {}).items()]
    out("Chat Completions (from `python -m traffic check-chat-completions`):")
    table(statuses)
    return {"appKey": app_key, "costExplorer": ce_result, "costExplorerNote": note, "curProfileRows": cur_rows,
            "chatCompletions": statuses}


def t4(cfg, sess, args) -> dict[str, Any]:
    price_in, price_out = cfg["pricing"]["inputPer1M"], cfg["pricing"]["outputPer1M"]
    if price_in is None or price_out is None:
        raise SystemExit("config.pricing.inputPer1M / outputPer1M are null — enter the us-east-1 rates from "
                         f"{cfg['pricing']['source']} (with readOn) before running t4.")
    day = pick_day(args.day, last_day_of_mode("direct"))
    start_s, end_s = logs_window(day)
    query = lab_query(cfg)
    logs = sess.client("logs")
    qid = logs.start_query(logGroupName=cfg["logGroupName"], startTime=start_s, endTime=end_s,
                           queryString=query)["queryId"]
    while True:
        res = logs.get_query_results(queryId=qid)
        if res["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        time.sleep(2)
    if res["status"] != "Complete":
        raise SystemExit(f"Logs Insights query ended with status {res['status']}")
    rows = [{f["field"]: f["value"] for f in r} for r in res["results"]]
    total_in = sum(float(r.get("totalInput") or 0) for r in rows)
    total_out = sum(float(r.get("totalOutput") or 0) for r in rows)
    estimate = total_in / 1e6 * float(price_in) + total_out / 1e6 * float(price_out)

    ce = sess.client("ce")
    _, billed_rows, note = bedrock_cost(ce, *day_window(day), None)
    billed = sum(r["amount"] for r in billed_rows)
    diff = estimate - billed
    pct = (diff / billed * 100) if billed else None

    out(f"Logs Insights {day} (UTC), filtered requestMetadata.lab = {cfg['traffic']['metadataLab']!r}:")
    table(rows, ["team", "feature", "modelId", "totalInput", "totalOutput", "calls"])
    out(CE_NOTE)
    table([{"estimateUsd": money(estimate), "billedUsd": money(billed), "differenceUsd": money(diff),
            "differencePct": f"{pct:.2f}" if pct is not None else "n/a (billed is 0)"}])
    return {"day": day.isoformat(), "pricing": cfg["pricing"], "logsInsights": rows, "totalInputTokens": total_in,
            "totalOutputTokens": total_out, "estimateUsd": estimate, "billedUsd": billed,
            "differenceUsd": diff, "differencePct": pct, "costExplorerNote": note}


def t5(cfg, sess, args) -> dict[str, Any]:
    ce = sess.client("ce")
    act = activation()
    act_at = activation_time(act)
    day = pick_day(args.day, first_day_of_mode())
    team_key = listed_key(cfg, "team", act)
    _, ce_result, note = bedrock_cost(ce, *day_window(day), team_key)
    out(CE_NOTE)
    out(f"Cost Explorer {day} grouped by {team_key!r} — raw group keys as returned (including the no-value group):")
    table([{**r, "amount": money(r["amount"])} for r in ce_result], ["date", "group", "amount", "unit"])

    outputs = lab_outputs(sess, cfg)
    df = load_cur(sess, cfg, outputs)
    work = df.copy()
    work["team"] = work[COL_TAGS].map(lambda t: principal_tag(tags_dict(t), cfg["tagKeys"]["team"]) or "(none)")
    work["hour"] = work[COL_USAGE_START].dt.strftime("%Y-%m-%dT%H:00Z")
    work["vsActivation"] = ("unknown" if act_at is None
                            else work[COL_USAGE_START].map(lambda t: "before" if t < act_at else "after"))
    grouped = work.groupby(["hour", "vsActivation", "team"])[COL_COST].sum().reset_index()
    cur_rows = [{"hour": r["hour"], "vsActivation": r["vsActivation"], "team": r["team"], "cost": money(r[COL_COST])}
                for _, r in grouped.sort_values("hour").iterrows()]
    out(f"CUR 2.0 hourly Bedrock rows vs activation ({utc_iso(act_at) if act_at else 'not recorded'}):")
    table(cur_rows)
    return {"day": day.isoformat(), "teamKey": team_key, "activatedAt": utc_iso(act_at) if act_at else None,
            "costExplorer": ce_result, "costExplorerNote": note, "curHourly": cur_rows}


def t6(cfg, sess, args) -> dict[str, Any]:
    outputs = lab_outputs(sess, cfg)
    df = load_cur(sess, cfg, outputs)
    with_principal = df[df[COL_PRINCIPAL].notna() & (df[COL_PRINCIPAL].astype(str) != "")]
    earliest_principal = with_principal[COL_USAGE_START].min() if len(with_principal) else None
    earliest_any = df[COL_USAGE_START].min() if len(df) else None
    try:
        stack = sess.client("cloudformation").describe_stacks(StackName=cfg["stackNames"]["curExport"])["Stacks"][0]
        export_created, created_via = stack["CreationTime"], "stack CreationTime"
    except Exception as exc:
        cli = read_json(RESULTS_DIR / "export-cli.json")  # written by scripts/create_export_cli.py create
        if not cli:
            raise SystemExit("No CurExport stack and no results/export-cli.json — was the export created?") from exc
        export_created, created_via = parse_ts(cli["writtenAt"]), "create_export_cli.py"
    calls = [parse_ts(r["timestamp"]) for r in run_records()]
    first_call = min(calls) if calls else None
    rows = [
        {"what": "first lab call recorded in runs/", "utc": utc_iso(first_call) if first_call else "none"},
        {"what": f"CUR export created ({created_via})", "utc": utc_iso(export_created)},
        {"what": f"earliest Bedrock CUR row with {COL_PRINCIPAL}",
         "utc": utc_iso(earliest_principal.to_pydatetime()) if earliest_principal is not None else "none"},
        {"what": "earliest Bedrock CUR row (any)",
         "utc": utc_iso(earliest_any.to_pydatetime()) if earliest_any is not None else "none"},
    ]
    table(rows)
    return {"timestamps": rows, "bedrockRows": len(df), "rowsWithPrincipal": len(with_principal)}


TESTS = {"t0": t0, "t1": t1, "t2": t2, "t3": t3, "t4": t4, "t5": t5, "t6": t6}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("test", choices=sorted(TESTS))
    parser.add_argument("--day", help="UTC day (YYYY-MM-DD) to read; defaults depend on the test and runs/")
    parser.add_argument("--profile")
    parser.add_argument("--config")
    parser.add_argument("--show-ids", action="store_true")
    args = parser.parse_args(argv)
    set_show_ids(args.show_ids)
    cfg = load_config(args.config)
    sess = session(cfg, args.profile)
    out(f"{cfg['traffic']['label']} — verify {args.test}")
    record = TESTS[args.test](cfg, sess, args)
    path: Path = write_json(RESULTS_DIR / f"{args.test}.json", {"test": args.test, **record})
    out(f"recorded: {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
