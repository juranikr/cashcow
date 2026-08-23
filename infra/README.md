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

GitHub deployment mirrors `cloudmiddle`: pushes to `main` that change
`runtime/**` are authenticated to AWS through repository-scoped OIDC, build
both commit-SHA and `latest` ECR tags, roll the singleton ECS service, wait for
stability, and verify the public health endpoint. GitHub stores only the
`AWS_ROLE_ARN`; Groq and runtime service tokens remain in AWS Secrets Manager.

Repository configuration:

- Secret: `AWS_ROLE_ARN` from `terraform output -raw github_actions_role_arn`
- Variables: `ENABLE_AWS_DEPLOY=true`, `ECR_REPOSITORY=cashcow-prod-runtime`,
  `ECS_CLUSTER=tourmiddle-dev-cluster`, `ECS_SERVICE=cashcow-prod-runtime`, and
  `RUNTIME_HEALTH_URL=https://d232kzujcg4ufp.cloudfront.net/cashcow/health`

The owner-only UI is hosted by Sites and deliberately remains a separate
release target. GitHub Actions validates every UI change; the Sites control
plane publishes the validated build because it owns the ChatGPT identity and
D1 bindings.
