#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { loadConfig, resolveConfigPath } from '../lib/config';
import { AttributionLabStack } from '../lib/attribution-lab-stack';
import { CurExportStack } from '../lib/cur-export-stack';

const app = new cdk.App();
const config = loadConfig(resolveConfigPath(app.node.tryGetContext('configPath')));
// Optional override for checks without editing the file: npx cdk synth -c budgetEmail=you@example.com
const budgetEmail: string | undefined = app.node.tryGetContext('budgetEmail');
const env = { account: process.env.CDK_DEFAULT_ACCOUNT, region: config.region };

// Day 0: roles, profile, logging, bucket, budget.
const lab = new AttributionLabStack(app, config.stackNames.lab, { env, config, budgetEmail });

// Day 1, after the first traffic run: the CUR 2.0 export (T6 needs usage to exist first).
new CurExportStack(app, config.stackNames.curExport, { env, config, bucket: lab.curBucket });
