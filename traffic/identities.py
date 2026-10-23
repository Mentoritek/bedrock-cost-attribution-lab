"""STS identities for each traffic mode.

One AssumeRole per identity per run, cached for the run: the AWS docs warn that calling
AssumeRole per request can exceed STS quotas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Identity:
    key: str  # cache key, e.g. "direct:search" or "gateway-per-user:alice"
    role_arn: str
    session_name: str
    session_tags: tuple[tuple[str, str], ...] = ()


@dataclass
class IdentityCache:
    sess: Any
    region: str
    _clients: dict[str, Any] = field(default_factory=dict)

    def client(self, identity: Identity, service: str = "bedrock-runtime"):
        cache_key = f"{identity.key}|{service}"
        if cache_key not in self._clients:
            creds = self.credentials(identity)
            self._clients[cache_key] = self.sess.client(
                service,
                region_name=self.region,
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
                config=_no_sdk_retries(),
            )
        return self._clients[cache_key]

    def credentials(self, identity: Identity) -> dict[str, str]:
        cache_key = f"{identity.key}|creds"
        if cache_key not in self._clients:
            kwargs: dict[str, Any] = {"RoleArn": identity.role_arn, "RoleSessionName": identity.session_name}
            if identity.session_tags:
                kwargs["Tags"] = [{"Key": k, "Value": v} for k, v in identity.session_tags]
            self._clients[cache_key] = self.sess.client("sts").assume_role(**kwargs)["Credentials"]
        return self._clients[cache_key]


def _no_sdk_retries():
    # invoke.py does its own exponential backoff on the transient errors Bedrock documents as retryable.
    from botocore.config import Config

    return Config(retries={"total_max_attempts": 1})


def direct_identity(cfg: dict[str, Any], outputs: dict[str, Any], team: str, run_id: str) -> Identity:
    return Identity(
        key=f"direct:{team}",
        role_arn=outputs["TeamRoleArns"][team],
        session_name=_session_name(f"{team}-{run_id}"),
    )


def gateway_shared_identity(cfg: dict[str, Any], outputs: dict[str, Any]) -> Identity:
    return Identity(
        key="gateway-shared",
        role_arn=outputs["GatewayRoleArn"],
        session_name=cfg["gateway"]["sharedSessionName"],
    )


def gateway_user_identity(cfg: dict[str, Any], outputs: dict[str, Any], user: dict[str, Any]) -> Identity:
    keys = cfg["tagKeys"]
    return Identity(
        key=f"gateway-per-user:{user['session']}",
        role_arn=outputs["GatewayRoleArn"],
        session_name=_session_name(user["session"]),
        session_tags=((keys["team"], user["team"]), (keys["user"], user["user"])),
    )


def _session_name(name: str) -> str:
    # STS AssumeRole RoleSessionName: 2-64 characters of [\w+=,.@-]
    allowed = "".join(ch if (ch.isascii() and ch.isalnum()) or ch in "+=,.@_-" else "-" for ch in name)
    return allowed[:64]
