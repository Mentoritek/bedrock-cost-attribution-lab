import * as cdk from 'aws-cdk-lib/core';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { AttributionLabStack } from '../lib/attribution-lab-stack';
import { CurExportStack } from '../lib/cur-export-stack';
import { foundationModelArn, LabConfig, loadConfig } from '../lib/config';

// Fake account for synth only — never a real one.
const ACCOUNT = '111111111111';

function testConfig(): LabConfig {
  const cfg = structuredClone(loadConfig());
  cfg.budget.email = 'lab-owner@example.com';
  return cfg;
}

function synth(cfg: LabConfig) {
  const app = new cdk.App();
  const env = { account: ACCOUNT, region: cfg.region };
  const lab = new AttributionLabStack(app, cfg.stackNames.lab, { env, config: cfg });
  const cur = new CurExportStack(app, cfg.stackNames.curExport, { env, config: cfg, bucket: lab.curBucket });
  return { lab: Template.fromStack(lab), cur: Template.fromStack(cur) };
}

function rolesByName(template: Template): Record<string, any> {
  const byName: Record<string, any> = {};
  for (const r of Object.values(template.findResources('AWS::IAM::Role'))) {
    byName[(r as any).Properties.RoleName] = (r as any).Properties;
  }
  return byName;
}

function tagMap(props: any): Record<string, string> {
  return Object.fromEntries((props.Tags ?? []).map((t: any) => [t.Key, t.Value]));
}

function trustActions(props: any): string[] {
  return props.AssumeRolePolicyDocument.Statement.flatMap((s: any) => [s.Action].flat());
}

describe('AttributionLabStack', () => {
  const cfg = testConfig();
  const { lab } = synth(cfg);
  const roles = rolesByName(lab);

  test('one role per configured team: team=<team>, path=direct, trust allows sts:AssumeRole only', () => {
    for (const team of cfg.teams) {
      const role = roles[`${cfg.roles.teamRolePrefix}${team.name}`];
      expect(role).toBeDefined();
      expect(tagMap(role)).toMatchObject({ [cfg.tagKeys.team]: team.name, [cfg.tagKeys.path]: cfg.roles.directPathTag });
      expect(trustActions(role)).toEqual(['sts:AssumeRole']);
    }
    const teamRoles = Object.keys(roles).filter((n) => n.startsWith(cfg.roles.teamRolePrefix));
    expect(teamRoles).toHaveLength(cfg.teams.length);
  });

  test('gateway role trust allows sts:AssumeRole and sts:TagSession; tags team=gateway, path=gateway', () => {
    const gw = roles[cfg.roles.gatewayRoleName];
    expect(gw).toBeDefined();
    expect(trustActions(gw).sort()).toEqual(['sts:AssumeRole', 'sts:TagSession']);
    expect(tagMap(gw)).toMatchObject({
      [cfg.tagKeys.team]: cfg.gateway.roleTag.team,
      [cfg.tagKeys.path]: cfg.gateway.roleTag.path,
    });
  });

  test('team and gateway roles may invoke the foundation model and the profile', () => {
    for (const name of [...cfg.teams.map((t) => `${cfg.roles.teamRolePrefix}${t.name}`), cfg.roles.gatewayRoleName]) {
      const stmt = roles[name].Policies[0].PolicyDocument.Statement.find((s: any) => s.Sid === 'InvokeLabModel');
      expect(stmt.Action).toEqual(['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream']);
      expect(stmt.Resource).toContain(foundationModelArn(cfg));
      expect(stmt.Resource).toHaveLength(2);
    }
  });

  test('application inference profile: configured name, CopyFrom gpt-oss-20b, app tag', () => {
    lab.hasResourceProperties('AWS::Bedrock::ApplicationInferenceProfile', {
      InferenceProfileName: cfg.profile.name,
      Description: cfg.profile.description,
      ModelSource: { CopyFrom: 'arn:aws:bedrock:us-east-1::foundation-model/openai.gpt-oss-20b-1:0' },
      Tags: Match.arrayWith([{ Key: cfg.tagKeys.app, Value: cfg.profile.appTag }]),
    });
  });

  test('custom resource calls PutModelInvocationLoggingConfiguration with text delivery and the log group', () => {
    const resources = lab.findResources('Custom::AWS');
    const create = Object.values(resources).map((r: any) => r.Properties.Create)[0];
    const serialized = JSON.stringify(create);
    expect(serialized).toContain('PutModelInvocationLoggingConfiguration');
    expect(serialized).toContain('\\"textDataDeliveryEnabled\\":true');
    expect(serialized).toContain(cfg.logGroupName);
    const del = JSON.stringify(Object.values(resources).map((r: any) => r.Properties.Delete)[0]);
    expect(del).toContain('DeleteModelInvocationLoggingConfiguration');
  });

  test('invocation log group and Bedrock logging role trust', () => {
    lab.hasResourceProperties('AWS::Logs::LogGroup', { LogGroupName: cfg.logGroupName, RetentionInDays: 30 });
    const logging = roles[cfg.roles.loggingRoleName];
    const stmt = logging.AssumeRolePolicyDocument.Statement[0];
    expect(stmt.Principal).toEqual({ Service: 'bedrock.amazonaws.com' });
    expect(stmt.Condition.StringEquals['aws:SourceAccount']).toBe(ACCOUNT);
    expect(stmt.Condition.ArnLike['aws:SourceArn']).toBe(`arn:aws:bedrock:us-east-1:${ACCOUNT}:*`);
  });

  test('bucket policy lets bcm-data-exports.amazonaws.com s3:PutObject with both conditions', () => {
    lab.hasResourceProperties('AWS::S3::BucketPolicy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: 'EnableAWSDataExportsToWriteToS3',
            Effect: 'Allow',
            Principal: { Service: 'bcm-data-exports.amazonaws.com' },
            Action: 's3:PutObject',
            Condition: {
              ArnLike: { 'aws:SourceArn': `arn:aws:bcm-data-exports:us-east-1:${ACCOUNT}:export/*` },
              StringEquals: { 'aws:SourceAccount': ACCOUNT },
            },
          }),
        ]),
      },
    });
    lab.hasResourceProperties('AWS::S3::Bucket', {
      OwnershipControls: { Rules: [{ ObjectOwnership: 'BucketOwnerEnforced' }] },
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true, BlockPublicPolicy: true, IgnorePublicAcls: true, RestrictPublicBuckets: true,
      },
    });
  });

  test('budget COST / MONTHLY / 25 USD with two email notifications', () => {
    lab.hasResourceProperties('AWS::Budgets::Budget', {
      Budget: {
        BudgetType: 'COST',
        TimeUnit: 'MONTHLY',
        BudgetLimit: { Amount: 25, Unit: 'USD' },
      },
      NotificationsWithSubscribers: [
        {
          Notification: { NotificationType: 'ACTUAL', ComparisonOperator: 'GREATER_THAN', Threshold: 80, ThresholdType: 'PERCENTAGE' },
          Subscribers: [{ SubscriptionType: 'EMAIL', Address: 'lab-owner@example.com' }],
        },
        {
          Notification: { NotificationType: 'FORECASTED', ComparisonOperator: 'GREATER_THAN', Threshold: 100, ThresholdType: 'PERCENTAGE' },
          Subscribers: [{ SubscriptionType: 'EMAIL', Address: 'lab-owner@example.com' }],
        },
      ],
    });
  });

  test('outputs that traffic/ and scripts/ read', () => {
    for (const name of ['TeamRoleArns', 'GatewayRoleArn', 'ProfileArn', 'LogGroupName', 'CurBucketName']) {
      lab.hasOutput(name, {});
    }
  });

  test('project tag on taggable resources', () => {
    lab.hasResourceProperties('AWS::S3::Bucket', {
      Tags: Match.arrayWith([{ Key: cfg.projectTag.key, Value: cfg.projectTag.value }]),
    });
  });

  test('synth fails with a clear message when budget.email is empty', () => {
    const empty = testConfig();
    empty.budget.email = '';
    const app = new cdk.App();
    expect(() => new AttributionLabStack(app, 'Empty', { env: { account: ACCOUNT, region: empty.region }, config: empty }))
      .toThrow(/config\.budget\.email is empty/);
  });

  test('removing a team from a copy of the config removes its role (nothing hard-coded)', () => {
    const smaller = testConfig();
    const removed = smaller.teams.pop()!;
    smaller.teams[0].weight += removed.weight;
    smaller.gateway.users = smaller.gateway.users.filter((u) => u.team !== removed.name);
    const names = Object.keys(rolesByName(synth(smaller).lab));
    expect(names).not.toContain(`${smaller.roles.teamRolePrefix}${removed.name}`);
    expect(names.filter((n) => n.startsWith(smaller.roles.teamRolePrefix))).toHaveLength(smaller.teams.length);
  });
});

describe('CurExportStack', () => {
  const cfg = testConfig();
  const { cur } = synth(cfg);

  test('CUR 2.0 export: IAM principal data, hourly, Parquet/Parquet/CUSTOM/OVERWRITE_REPORT, SYNCHRONOUS', () => {
    cur.hasResourceProperties('AWS::BCMDataExports::Export', {
      Export: {
        Name: cfg.curExport.name,
        DataQuery: {
          QueryStatement: Match.stringLikeRegexp('line_item_iam_principal.*tags FROM COST_AND_USAGE_REPORT'),
          TableConfigurations: {
            COST_AND_USAGE_REPORT: Match.objectLike({
              INCLUDE_IAM_PRINCIPAL_DATA: 'TRUE',
              TIME_GRANULARITY: 'HOURLY',
              INCLUDE_RESOURCES: 'TRUE',
            }),
          },
        },
        DestinationConfigurations: {
          S3Destination: Match.objectLike({
            S3Prefix: cfg.curExport.s3Prefix,
            S3Region: 'us-east-1',
            S3OutputConfigurations: {
              OutputType: 'CUSTOM',
              Format: 'PARQUET',
              Compression: 'PARQUET',
              Overwrite: 'OVERWRITE_REPORT',
            },
          }),
        },
        RefreshCadence: { Frequency: 'SYNCHRONOUS' },
      },
    });
    cur.hasOutput('ExportArn', {});
  });
});
