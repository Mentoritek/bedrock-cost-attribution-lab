"""python -m traffic <run|smoke|check-chat-completions> — see README "Steps"."""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from . import identities as ids
from .common import RESULTS_DIR, RUNS_DIR, lab_outputs, session, utc_iso, utc_now, write_json
from .config import load_config
from .invoke import append_jsonl, invoke_once, model_for
from .redact import err, out, set_show_ids, table
from .schedule import assign_users, build_plan, plan_table

MODES = ("direct", "gateway-shared", "gateway-per-user")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m traffic", description="MT-01 Bedrock lab traffic generator")
    parser.add_argument("--profile", help="AWS CLI profile (default: standard credential chain)")
    parser.add_argument("--config", help="config file (default: $LAB_CONFIG, else config/lab.config.local.json "
                        "if present, else config/lab.config.json)")
    parser.add_argument("--show-ids", action="store_true", help="do not mask 12-digit account IDs")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="send the full schedule in one mode")
    run.add_argument("--mode", choices=MODES, required=True)
    run.add_argument("--run-id", help="label for this run (default: UTC timestamp)")
    run.add_argument("--dry-run", action="store_true", help="print the plan and exit (no AWS calls)")

    sub.add_parser("smoke", help="one call per team role, one through the gateway, one through the profile")
    sub.add_parser("check-chat-completions", help="T3: Chat Completions naming the model vs the profile")

    args = parser.parse_args(argv)
    set_show_ids(args.show_ids)
    cfg = load_config(args.config)

    if args.command == "run":
        return cmd_run(cfg, args)
    if args.command == "smoke":
        return cmd_smoke(cfg, args)
    return cmd_chat_completions(cfg, args)


def cmd_run(cfg: dict[str, Any], args) -> int:
    run_id = args.run_id or utc_now().strftime("run-%Y%m%dT%H%M%SZ")
    plan = build_plan(cfg)
    users = assign_users(plan, cfg["gateway"]["users"]) if args.mode != "direct" else [None] * len(plan)

    out(f"{cfg['traffic']['label']} — mode={args.mode} run_id={run_id} calls={len(plan)} seed={cfg['traffic']['seed']}")
    table(plan_table(plan), ["team", "feature", "calls"])
    if args.dry_run:
        return 0

    sess = session(cfg, args.profile)
    outputs = lab_outputs(sess, cfg)
    cache = ids.IdentityCache(sess, cfg["region"])
    log_path = RUNS_DIR / f"{run_id}.jsonl"
    pause = float(cfg["traffic"]["pauseSeconds"])
    failures = 0

    for call, user in zip(plan, users, strict=True):
        if args.mode == "direct":
            identity = ids.direct_identity(cfg, outputs, call.team, run_id)
        elif args.mode == "gateway-shared":
            identity = ids.gateway_shared_identity(cfg, outputs)
        else:
            identity = ids.gateway_user_identity(cfg, outputs, user)
        record = invoke_once(
            cache.client(identity), cfg,
            model_id=model_for(cfg, outputs, call.feature),
            team=call.team, feature=call.feature, run_id=run_id, mode=args.mode,
            index=call.index, user=user["user"] if user else None,
        )
        record["sessionName"] = identity.session_name
        append_jsonl(log_path, record)
        if record["error"]:
            failures += 1
            err(f"call {call.index} {call.team}/{call.feature}: {record['error']}")
        if (call.index + 1) % 50 == 0:
            out(f"  {call.index + 1}/{len(plan)} calls sent ({failures} failed)")
        time.sleep(pause)

    out(f"done: {len(plan)} calls, {failures} failed — {log_path.relative_to(RUNS_DIR.parent)}")
    return 1 if failures else 0


def cmd_smoke(cfg: dict[str, Any], args) -> int:
    sess = session(cfg, args.profile)
    outputs = lab_outputs(sess, cfg)
    cache = ids.IdentityCache(sess, cfg["region"])
    run_id = utc_now().strftime("smoke-%Y%m%dT%H%M%SZ")
    first_team = cfg["teams"][0]
    checks: list[tuple[str, ids.Identity, str, str, str]] = []
    for team in cfg["teams"]:
        identity = ids.direct_identity(cfg, outputs, team["name"], run_id)
        checks.append((f"team:{team['name']}", identity, cfg["model"]["foundationModelId"],
                       team["name"], team["features"][0]))
    checks.append(("gateway", ids.gateway_shared_identity(cfg, outputs), cfg["model"]["foundationModelId"],
                   first_team["name"], first_team["features"][0]))
    route_team = next(t for t in cfg["teams"] if cfg["profile"]["routeFeature"] in t["features"])
    checks.append(("profile", ids.direct_identity(cfg, outputs, route_team["name"], run_id), outputs["ProfileArn"],
                   route_team["name"], cfg["profile"]["routeFeature"]))

    rows, records = [], []
    for label, identity, model_id, team, feature in checks:
        try:
            client = cache.client(identity)
            record = invoke_once(client, cfg, model_id=model_id, team=team, feature=feature,
                                 run_id=run_id, mode="smoke", index=0)
        except Exception as exc:  # AssumeRole failure
            record = {"timestamp": utc_iso(), "modelId": model_id, "requestId": None, "error": str(exc)}
        record["identity"] = label
        records.append(record)
        append_jsonl(RUNS_DIR / f"{run_id}.jsonl", record)
        rows.append({"identity": label, "result": "OK" if not record["error"] else "FAIL",
                     "requestId": record.get("requestId") or "", "error": record["error"] or ""})
    table(rows, ["identity", "result", "requestId", "error"])
    write_json(RESULTS_DIR / "smoke.json", {"runId": run_id, "calls": records})
    return 0 if all(r["result"] == "OK" for r in rows) else 1


def cmd_chat_completions(cfg: dict[str, Any], args) -> int:
    from botocore.credentials import Credentials

    from .chat_completions_check import run_check

    sess = session(cfg, args.profile)
    outputs = lab_outputs(sess, cfg)
    cache = ids.IdentityCache(sess, cfg["region"])
    team = cfg["roles"]["chatCompletionsTeam"]
    raw = cache.credentials(ids.direct_identity(cfg, outputs, team, utc_now().strftime("t3-%Y%m%dT%H%M%SZ")))
    creds = Credentials(raw["AccessKeyId"], raw["SecretAccessKey"], raw["SessionToken"])
    result = run_check(creds, cfg["region"], cfg["model"]["foundationModelId"], outputs["ProfileArn"],
                       int(cfg["model"]["maxTokens"]))
    result["team"] = team
    rows = [{"request": label, "model": r["model"], "status": r["status"]}
            for label, r in result["results"].items()]
    table(rows, ["request", "model", "status"])
    for label, r in result["results"].items():
        if r["error"]:
            out(f"{label} error body: {r['error'][:500]}")
    path = write_json(RESULTS_DIR / "t3-chat-completions.json", result)
    out(f"recorded: {path.relative_to(RESULTS_DIR.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
