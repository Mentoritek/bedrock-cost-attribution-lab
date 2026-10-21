"""Loads and validates config/lab.config.json (the same file the CDK app reads)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "lab.config.json"
# Git-ignored copy with your own email and prices; used instead of the default when it exists.
LOCAL_CONFIG_PATH = REPO_ROOT / "config" / "lab.config.local.json"


class ConfigError(ValueError):
    pass


def config_path(override: str | None = None, local: Path | None = None) -> Path:
    """--config wins, then $LAB_CONFIG, then config/lab.config.local.json if it exists, then the repo default."""
    chosen = override or os.environ.get("LAB_CONFIG")
    if chosen:
        return Path(chosen).resolve()
    local = local or LOCAL_CONFIG_PATH
    return local if local.exists() else DEFAULT_CONFIG_PATH


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    p = config_path(str(path) if path else None)
    with p.open(encoding="utf-8") as fh:
        cfg = json.load(fh)
    validate(cfg)
    return cfg


def all_features(cfg: dict[str, Any]) -> list[str]:
    return [f for team in cfg["teams"] for f in team["features"]]


def team_names(cfg: dict[str, Any]) -> list[str]:
    return [t["name"] for t in cfg["teams"]]


def foundation_model_arn(cfg: dict[str, Any]) -> str:
    return f"arn:aws:bedrock:{cfg['region']}::foundation-model/{cfg['model']['foundationModelId']}"


def validate(cfg: dict[str, Any]) -> None:
    """Same rules as cdk/lib/config.ts (spec section 5)."""
    teams = cfg.get("teams") or []
    if not teams:
        raise ConfigError("config: at least one team is required")

    names = [t["name"] for t in teams]
    if len(set(names)) != len(names):
        raise ConfigError("config: team names must be unique")

    total = sum(float(t["weight"]) for t in teams)
    if abs(total - 1.0) > 0.001:
        raise ConfigError(f"config: team weights must sum to 1.0 (±0.001); got {total:.4f}")

    for t in teams:
        if not t.get("features"):
            raise ConfigError(f"config: team {t['name']!r} needs at least one feature")

    for u in cfg["gateway"]["users"]:
        if u["team"] not in names:
            raise ConfigError(f"config: gateway user {u['user']!r} names unknown team {u['team']!r}")

    features = all_features(cfg)
    if cfg["profile"]["routeFeature"] not in features:
        raise ConfigError(
            f"config: profile.routeFeature {cfg['profile']['routeFeature']!r} does not belong to any team"
        )

    keys = list(cfg["tagKeys"].values())
    if any(not str(k).strip() for k in keys):
        raise ConfigError("config: tag keys must be non-empty")
    if len(set(keys)) != len(keys):
        raise ConfigError("config: tag keys must be distinct")

    if int(cfg["traffic"]["callsPerRun"]) < len(features):
        raise ConfigError(
            f"config: traffic.callsPerRun ({cfg['traffic']['callsPerRun']}) must be >= "
            f"number of features ({len(features)})"
        )

    if cfg["roles"]["chatCompletionsTeam"] not in names:
        raise ConfigError("config: roles.chatCompletionsTeam must name a configured team")
