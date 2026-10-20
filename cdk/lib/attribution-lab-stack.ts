import * as cdk from 'aws-cdk-lib/core';
import * as bedrock from 'aws-cdk-lib/aws-bedrock';
import * as budgets from 'aws-cdk-lib/aws-budgets';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';
import { foundationModelArn, LabConfig, requireBudgetEmail } from './config';

export interface AttributionLabStackProps extends cdk.StackProps {
  config: LabConfig;
  /** Overrides config.budget.email (cdk context `budgetEmail`). */
  budgetEmail?: string;
}

/** Day-0 stack (spec section 4.1). Every name, tag and number comes from config/lab.config.json. */
export class AttributionLabStack extends cdk.Stack {
  readonly curBucket: s3.Bucket;
  readonly teamRoles: Record<string, iam.Role> = {};
  readonly gatewayRole: iam.Role;
  readonly profile: bedrock.CfnApplicationInferenceProfile;

  constructor(scope: Construct, id: string, props: AttributionLabStackProps) {
    super(scope, id, props);
    const cfg = props.config;
    const budgetEmail = requireBudgetEmail(cfg, props.budgetEmail);
    cdk.Tags.of(this).add(cfg.projectTag.key, cfg.projectTag.value);

    // (c) Application inference profile — L1, no alpha module.
    const modelArn = foundationModelArn(cfg);
    this.profile = new bedrock.CfnApplicationInferenceProfile(this, 'SupportAssistantProfile', {
      inferenceProfileName: cfg.profile.name,
      description: cfg.profile.description,
      modelSource: { copyFrom: modelArn },
      tags: [{ key: cfg.tagKeys.app, value: cfg.profile.appTag }],
    });
    const profileArn = this.profile.attrInferenceProfileArn;

    // Invoking through an application inference profile also needs the foundation model in the policy
    // (Bedrock "Prerequisites for inference profiles"), so both are granted.
    const invokePolicy = new iam.PolicyDocument({
      statements: [
        new iam.PolicyStatement({
          sid: 'InvokeLabModel',
          actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
          resources: [modelArn, profileArn],
        }),
        new iam.PolicyStatement({
          sid: 'ReadLabProfile',
          actions: ['bedrock:GetInferenceProfile'],
          resources: [profileArn],
        }),
      ],
    });

    // (a) Team roles — principal tags team=<team>, path=direct.
    for (const team of cfg.teams) {
      const role = new iam.Role(this, `TeamRole-${team.name}`, {
        roleName: `${cfg.roles.teamRolePrefix}${team.name}`,
        assumedBy: new iam.AccountRootPrincipal(),
        description: `MT-01 sample team role (${team.name})`,
        inlinePolicies: { BedrockLabInvoke: invokePolicy },
      });
      cdk.Tags.of(role).add(cfg.tagKeys.team, team.name);
      cdk.Tags.of(role).add(cfg.tagKeys.path, cfg.roles.directPathTag);
      this.teamRoles[team.name] = role;
    }

    // (b) Gateway role — trust also allows sts:TagSession for per-user session tags (T2).
    this.gatewayRole = new iam.Role(this, 'GatewayRole', {
      roleName: cfg.roles.gatewayRoleName,
      assumedBy: new iam.AccountRootPrincipal().withSessionTags(),
      description: 'MT-01 sample shared LLM gateway role',
      inlinePolicies: { BedrockLabInvoke: invokePolicy },
    });
    cdk.Tags.of(this.gatewayRole).add(cfg.tagKeys.team, cfg.gateway.roleTag.team);
    cdk.Tags.of(this.gatewayRole).add(cfg.tagKeys.path, cfg.gateway.roleTag.path);

    // (d) Invocation log group.
    const logGroup = new logs.LogGroup(this, 'InvocationLogGroup', {
      logGroupName: cfg.logGroupName,
      retention: retentionFromDays(cfg.logging.retentionDays),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // (e) Role Bedrock assumes to write invocation logs (trust shape from the model invocation logging page).
    const loggingRole = new iam.Role(this, 'InvocationLoggingRole', {
      roleName: cfg.roles.loggingRoleName,
      assumedBy: new iam.ServicePrincipal('bedrock.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': this.account },
          ArnLike: { 'aws:SourceArn': `arn:aws:bedrock:${this.region}:${this.account}:*` },
        },
      }),
      description: 'Lets Amazon Bedrock write model invocation logs for the MT-01 lab',
      inlinePolicies: {
        WriteInvocationLogs: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
              resources: [
                `arn:aws:logs:${this.region}:${this.account}:log-group:${cfg.logGroupName}:log-stream:aws/bedrock/modelinvocations`,
              ],
            }),
          ],
        }),
      },
    });

    // (f) Model invocation logging — no CloudFormation resource exists, so an AwsCustomResource calls the API.
    const loggingCall: cr.AwsSdkCall = {
      service: 'bedrock',
      action: 'PutModelInvocationLoggingConfiguration',
      parameters: {
        loggingConfig: {
          cloudWatchConfig: { logGroupName: cfg.logGroupName, roleArn: loggingRole.roleArn },
          textDataDeliveryEnabled: true,
          imageDataDeliveryEnabled: false,
          embeddingDataDeliveryEnabled: false,
          videoDataDeliveryEnabled: false,
          audioDataDeliveryEnabled: false,
        },
      },
      physicalResourceId: cr.PhysicalResourceId.of(cfg.logging.customResourceId),
    };
    const invocationLogging = new cr.AwsCustomResource(this, 'InvocationLoggingConfig', {
      onCreate: loggingCall,
      onUpdate: loggingCall,
      onDelete: { service: 'bedrock', action: 'DeleteModelInvocationLoggingConfiguration' },
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          actions: ['bedrock:PutModelInvocationLoggingConfiguration', 'bedrock:DeleteModelInvocationLoggingConfiguration'],
          resources: ['*'],
        }),
        new iam.PolicyStatement({ actions: ['iam:PassRole'], resources: [loggingRole.roleArn] }),
      ]),
      // The Lambda docs don't list which SDK v3 clients the Node runtime bundles, so install the latest SDK
      // rather than assume @aws-sdk/client-bedrock is present (see README "Sources").
      installLatestAwsSdk: true,
    });
    invocationLogging.node.addDependency(logGroup, loggingRole);

    // (g) CUR 2.0 bucket — the export itself is created on day 1 by CurExportStack.
    this.curBucket = new s3.Bucket(this, 'CurBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      encryption: s3.BucketEncryption.S3_MANAGED,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });
    this.curBucket.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'EnableAWSDataExportsToWriteToS3',
        principals: [new iam.ServicePrincipal('bcm-data-exports.amazonaws.com')],
        actions: ['s3:PutObject'],
        resources: [this.curBucket.arnForObjects('*')],
        conditions: {
          ArnLike: { 'aws:SourceArn': `arn:aws:bcm-data-exports:${this.region}:${this.account}:export/*` },
          StringEquals: { 'aws:SourceAccount': this.account },
        },
      }),
    );

    // (h) Budget alert.
    const subscriber = { subscriptionType: 'EMAIL', address: budgetEmail };
    new budgets.CfnBudget(this, 'LabBudget', {
      budget: {
        budgetName: cfg.budget.name,
        budgetType: 'COST',
        timeUnit: 'MONTHLY',
        budgetLimit: { amount: cfg.budget.limitUsd, unit: 'USD' },
      },
      notificationsWithSubscribers: [
        {
          notification: {
            notificationType: 'ACTUAL',
            comparisonOperator: 'GREATER_THAN',
            threshold: cfg.budget.actualThresholdPercent,
            thresholdType: 'PERCENTAGE',
          },
          subscribers: [subscriber],
        },
        {
          notification: {
            notificationType: 'FORECASTED',
            comparisonOperator: 'GREATER_THAN',
            threshold: cfg.budget.forecastThresholdPercent,
            thresholdType: 'PERCENTAGE',
          },
          subscribers: [subscriber],
        },
      ],
    });

    // (i) Outputs — traffic/ and scripts/ read these with DescribeStacks.
    const roleArns: Record<string, string> = {};
    for (const [team, role] of Object.entries(this.teamRoles)) roleArns[team] = role.roleArn;
    new cdk.CfnOutput(this, 'TeamRoleArns', { value: this.toJsonString(roleArns) });
    new cdk.CfnOutput(this, 'GatewayRoleArn', { value: this.gatewayRole.roleArn });
    new cdk.CfnOutput(this, 'ProfileArn', { value: profileArn });
    new cdk.CfnOutput(this, 'LogGroupName', { value: logGroup.logGroupName });
    new cdk.CfnOutput(this, 'CurBucketName', { value: this.curBucket.bucketName });
  }
}

function retentionFromDays(days: number): logs.RetentionDays {
  const match = Object.values(logs.RetentionDays).find((v) => v === days);
  if (match === undefined) throw new Error(`config: logging.retentionDays ${days} is not a CloudWatch Logs retention value`);
  return match as logs.RetentionDays;
}
