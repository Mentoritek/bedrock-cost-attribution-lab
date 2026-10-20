"""Day-0 checks before `cdk deploy` (spec section 6.1). Writes results/preflight.json."""

from __future__ import annotations

import argparse
import sys

import _bootstrap  # noqa: F401

from traffic.common import RESULTS_DIR, error_code, session, write_json
from traffic.config import load_config
from traffic.redact import err, out, set_show_ids

MEMBER_REASON = (
    "This is a member account of an AWS Organization. Cost allocation tags are managed at the payer "
    "account level, so this lab cannot activate them here. Use a standalone account (see README Prerequisites)."
)


def account_type(sess, caller_account: str) -> dict:
    org = sess.client("organizations")
    try:
        desc = org.describe_organization()["Organization"]
    except Exception as exc:
        if error_code(exc) == "AWSOrganizationsNotInUseException":
            return {"accountType": "standalone"}
        raise
    kind = "management" if desc.get("MasterAccountId") == caller_account else "member"
    return {"accountType": kind, "organizationId": desc.get("Id")}


def logging_check(sess, cfg) -> dict:
    current = sess.client("bedrock").get_model_invocation_logging_configuration().get("loggingConfig")
    if not current:
        return {"existing": False, "ownedByLab": False}
    group = (current.get("cloudWatchConfig") or {}).get("logGroupName")
    return {"existing": True, "ownedByLab": group == cfg["logGroupName"], "logGroupName": group}


def converse_has_request_metadata(sess) -> bool:
    client = sess.client("bedrock-runtime")
    members = client.meta.service_model.operation_model("Converse").input_shape.members
    return "requestMetadata" in members


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--config")
    parser.add_argument("--show-ids", action="store_true")
    args = parser.parse_args(argv)
    set_show_ids(args.show_ids)
    cfg = load_config(args.config)
    sess = session(cfg, args.profile)
    problems: list[str] = []

    ident = sess.client("sts").get_caller_identity()
    out(f"Caller: {ident['Arn']} (account {ident['Account']})")

    acct = account_type(sess, ident["Account"])
    out(f"Account type: {acct['accountType']}")
    if acct["accountType"] == "member":
        problems.append(MEMBER_REASON)

    logging = logging_check(sess, cfg)
    if logging["existing"] and not logging["ownedByLab"]:
        problems.append(
            "This account/Region already has Bedrock model invocation logging configured "
            f"(log group {logging.get('logGroupName')!r}); deploying would overwrite it."
        )
    owner = "none" if not logging["existing"] else ("this lab" if logging["ownedByLab"] else "OTHER")
    out(f"Invocation logging: {owner}")

    has_meta = converse_has_request_metadata(sess)
    out(f"botocore Converse supports requestMetadata: {has_meta}")
    if not has_meta:
        problems.append("Installed boto3/botocore has no requestMetadata on Converse — "
                        "upgrade boto3 (pip install -r requirements.txt).")

    if not cfg["budget"].get("email"):
        problems.append("config.budget.email is empty — set it in config/lab.config.json before deploying.")
    missing_pricing = [k for k in ("inputPer1M", "outputPer1M", "source", "readOn") if k not in cfg["pricing"]]
    if missing_pricing:
        problems.append(f"config.pricing is missing fields: {missing_pricing}")
    elif cfg["pricing"]["inputPer1M"] is None:
        out("Pricing: not entered yet (fine on day 0; verify.py t4 needs it).")

    write_json(RESULTS_DIR / "preflight.json", {
        "callerArn": ident["Arn"],
        "account": ident["Account"],
        **acct,
        "invocationLogging": logging,
        "converseRequestMetadata": has_meta,
        "problems": problems,
    })
    for p in problems:
        err(f"FAIL: {p}")
    out("Preflight OK." if not problems else f"Preflight found {len(problems)} problem(s).")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
