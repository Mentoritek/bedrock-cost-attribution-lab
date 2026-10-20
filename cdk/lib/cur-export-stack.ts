import * as cdk from 'aws-cdk-lib/core';
import * as bcm from 'aws-cdk-lib/aws-bcmdataexports';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';
import { LabConfig } from './config';

export interface CurExportStackProps extends cdk.StackProps {
  config: LabConfig;
  bucket: s3.IBucket;
}

// CUR 2.0 columns, names from the CUR 2.0 table dictionary.
export const CUR_COLUMNS = [
  'bill_billing_period_start_date',
  'line_item_usage_start_date',
  'line_item_usage_end_date',
  'line_item_usage_account_id',
  'line_item_product_code',
  'line_item_usage_type',
  'line_item_operation',
  'line_item_resource_id',
  'line_item_usage_amount',
  'line_item_unblended_cost',
  'line_item_iam_principal',
  'tags',
];

export const CUR_TABLE = 'COST_AND_USAGE_REPORT';

/** Day-1 stack (spec section 4.2): deployed after day-0 traffic so T6 can check identity coverage. */
export class CurExportStack extends cdk.Stack {
  readonly export: bcm.CfnExport;

  constructor(scope: Construct, id: string, props: CurExportStackProps) {
    super(scope, id, props);
    const cfg = props.config;
    cdk.Tags.of(this).add(cfg.projectTag.key, cfg.projectTag.value);

    this.export = new bcm.CfnExport(this, 'Cur2Export', {
      export: {
        name: cfg.curExport.name,
        dataQuery: {
          queryStatement: `SELECT ${CUR_COLUMNS.join(', ')} FROM ${CUR_TABLE}`,
          tableConfigurations: {
            [CUR_TABLE]: {
              TIME_GRANULARITY: cfg.curExport.timeGranularity,
              INCLUDE_RESOURCES: 'TRUE',
              INCLUDE_IAM_PRINCIPAL_DATA: 'TRUE',
              INCLUDE_SPLIT_COST_ALLOCATION_DATA: 'FALSE',
              INCLUDE_CAPACITY_RESERVATION_DATA: 'FALSE',
              INCLUDE_MANUAL_DISCOUNT_COMPATIBILITY: 'FALSE',
            },
          },
        },
        destinationConfigurations: {
          s3Destination: {
            s3Bucket: props.bucket.bucketName,
            s3Prefix: cfg.curExport.s3Prefix,
            s3Region: cfg.region,
            s3OutputConfigurations: {
              outputType: 'CUSTOM',
              format: 'PARQUET',
              compression: 'PARQUET',
              overwrite: 'OVERWRITE_REPORT',
            },
          },
        },
        refreshCadence: { frequency: 'SYNCHRONOUS' },
      },
    });

    new cdk.CfnOutput(this, 'ExportArn', { value: this.export.attrExportArn });
  }
}
