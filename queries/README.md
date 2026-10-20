# Logs Insights queries

These two queries read the Bedrock model invocation logs that the lab writes to `/mentoritek/bedrock-cost-lab/invocations`.

| File | What it totals | Source |
|---|---|---|
| [`logs-insights/tokens-by-team-feature.txt`](logs-insights/tokens-by-team-feature.txt) | Input/output tokens and calls per `requestMetadata.team` × `requestMetadata.feature` × `modelId`, lab traffic only | Adapted from the [cost attribution FAQ](https://docs.aws.amazon.com/bedrock/latest/userguide/cost-mgmt-faq.html) query |
| [`logs-insights/tokens-by-principal.txt`](logs-insights/tokens-by-principal.txt) | Tokens and calls per calling IAM principal (`identity.arn`) | Unchanged from the [model invocation logging](https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html) page |

## Running them in the console

1. Open **CloudWatch → Logs → Logs Insights** in `us-east-1`.
2. Select the log group `/mentoritek/bedrock-cost-lab/invocations`.
3. Set the time range, for example one UTC day.
4. Paste the contents of one `.txt` file and choose **Run query**.

Things to know:
- `modelId` holds the profile's ID, not the model's, for calls made through the application inference profile (`reply-draft` in the sample config). That's why those rows appear separately.
- In `tokens-by-principal`, the gateway's per-user sessions show up as separate `identity.arn` values, because the session name is part of the assumed-role ARN.
- The team × feature query filters on `requestMetadata.lab = "mt-01"`. `scripts/verify.py t4` replaces that value with `traffic.metadataLab` from the config, so change both if you rename the lab.
