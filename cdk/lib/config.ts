import * as fs from 'node:fs';
import * as path from 'node:path';

// Typed view of config/lab.config.json. The Python side (traffic/config.py) reads the same
// file and applies the same validation rules (spec section 5).

export interface TeamConfig {
  name: string;
  weight: number;
  features: string[];
}

export interface GatewayUser {
  user: string;
  session: string;
  team: string;
}

export interface LabConfig {
  region: string;
  stackNames: { lab: string; curExport: string };
  projectTag: { key: string; value: string };
  model: { foundationModelId: string; maxTokens: number };
  pricing: {
    inputPer1M: number | null;
    outputPer1M: number | null;
    source: string;
    readOn: string | null;
    note?: string;
  };
  tagKeys: { team: string; path: string; user: string; app: string };
  teams: TeamConfig[];
  roles: {
    teamRolePrefix: string;
    directPathTag: string;
    gatewayRoleName: string;
    loggingRoleName: string;
    chatCompletionsTeam: string;
  };
  gateway: {
    roleTag: { team: string; path: string };
    sharedSessionName: string;
    users: GatewayUser[];
  };
  profile: { name: string; description: string; appTag: string; routeFeature: string };
  traffic: {
    callsPerRun: number;
    seed: number;
    pauseSeconds: number;
    maxAttempts: number;
    label: string;
    metadataLab: string;
  };
  budget: {
    name: string;
    limitUsd: number;
    email: string;
    actualThresholdPercent: number;
    forecastThresholdPercent: number;
  };
  logGroupName: string;
  logging: { retentionDays: number; customResourceId: string };
  curExport: { name: string; s3Prefix: string; timeGranularity: string };
}

export const DEFAULT_CONFIG_PATH = path.resolve(__dirname, '..', '..', 'config', 'lab.config.json');
/** Git-ignored copy with your own email and prices; used instead of the default when it exists. */
export const LOCAL_CONFIG_PATH = path.resolve(__dirname, '..', '..', 'config', 'lab.config.local.json');

/**
 * `-c configPath=...` wins, then $LAB_CONFIG, then config/lab.config.local.json if it exists,
 * then config/lab.config.json.
 */
export function resolveConfigPath(contextPath?: string, localPath: string = LOCAL_CONFIG_PATH): string {
  const chosen = contextPath ?? process.env.LAB_CONFIG;
  if (chosen) return path.resolve(chosen);
  return fs.existsSync(localPath) ? localPath : DEFAULT_CONFIG_PATH;
}

export function loadConfig(file: string = DEFAULT_CONFIG_PATH): LabConfig {
  const cfg = JSON.parse(fs.readFileSync(file, 'utf-8')) as LabConfig;
  validateConfig(cfg);
  return cfg;
}

export function validateConfig(cfg: LabConfig): void {
  const fail = (msg: string): never => {
    throw new Error(`config: ${msg}`);
  };

  if (!cfg.teams?.length) fail('at least one team is required');
  const names = cfg.teams.map((t) => t.name);
  if (new Set(names).size !== names.length) fail('team names must be unique');

  const total = cfg.teams.reduce((sum, t) => sum + Number(t.weight), 0);
  if (Math.abs(total - 1) > 0.001) fail(`team weights must sum to 1.0 (±0.001); got ${total.toFixed(4)}`);

  for (const t of cfg.teams) {
    if (!t.features?.length) fail(`team '${t.name}' needs at least one feature`);
  }

  for (const u of cfg.gateway.users) {
    if (!names.includes(u.team)) fail(`gateway user '${u.user}' names unknown team '${u.team}'`);
  }

  const features = allFeatures(cfg);
  if (!features.includes(cfg.profile.routeFeature)) {
    fail(`profile.routeFeature '${cfg.profile.routeFeature}' does not belong to any team`);
  }

  const keys = Object.values(cfg.tagKeys);
  if (keys.some((k) => !String(k).trim())) fail('tag keys must be non-empty');
  if (new Set(keys).size !== keys.length) fail('tag keys must be distinct');

  if (cfg.traffic.callsPerRun < features.length) {
    fail(`traffic.callsPerRun (${cfg.traffic.callsPerRun}) must be >= number of features (${features.length})`);
  }

  if (!names.includes(cfg.roles.chatCompletionsTeam)) fail('roles.chatCompletionsTeam must name a configured team');
}

/** Deploy-time requirement, checked when the stacks are built (cdk synth fails with this message). */
export function requireBudgetEmail(cfg: LabConfig, override?: string): string {
  const email = (override ?? cfg.budget.email)?.trim();
  if (!email) {
    throw new Error(
      'config.budget.email is empty. Set it in config/lab.config.json before synth/deploy — ' +
        'the $' + cfg.budget.limitUsd + ' budget alert emails this address.',
    );
  }
  return email;
}

export function allFeatures(cfg: LabConfig): string[] {
  return cfg.teams.flatMap((t) => t.features);
}

export function foundationModelArn(cfg: LabConfig): string {
  return `arn:aws:bedrock:${cfg.region}::foundation-model/${cfg.model.foundationModelId}`;
}
