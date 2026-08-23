# Cashcow persistent runtime

The UI remains owner-only on Sites. A dedicated ECS Fargate service owns the
world clock and persists canonical snapshots, jobs, events, reports, messages,
and whiteboard revisions in DynamoDB. Sites calls the runtime through the
existing HTTPS CloudFront → shared ALB path using a secret service token.

The stack deliberately reuses only the `cloudmiddle` VPC, public subnets, ECS
cluster, ALB, and HTTPS CloudFront distribution. Its task, security group,
target group, listener rule, ECR repository, DynamoDB table, IAM roles, logs,
and secret are Cashcow-specific.

Deployment order:

1. Initialize Terraform with the separate `cashcow/prod/terraform.tfstate` key.
2. Create ECR/DynamoDB/secret prerequisites.
3. Put `GROQ_API_KEY` and a random `RUNTIME_SERVICE_TOKEN` in the Cashcow secret.
4. Build `runtime/Dockerfile`, push `:latest`, and apply the full stack.
5. Configure Sites with the HTTPS runtime base URL and the same service token.

