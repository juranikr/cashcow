# Cashcow AWS production runtime

The owner-only UI is released separately. This Terraform stack owns the
persistent runtime and the isolated public-HTTPS browser boundary in
`ap-northeast-2`.

```text
CloudFront -> shared ALB -> cashcow-prod-runtime (shared ECS Fargate cluster)
                              | task role: DynamoDB only
                              | secrets: Groq + runtime service token
                              v
                         internal NLB:8001
                              v
                    browser worker (dedicated ECS EC2 cluster)
                      - one hardened AL2023 t3.medium host
                      - no public listener, SSH key, task role, or secret
                      - pwuser + cap-drop ALL + read-only rootfs
                      - Chromium sandbox + pinned Playwright seccomp
                      - host firewall: public TCP/443 only from containers

cashcow-prod-world (DynamoDB, PITR + deletion protection)
```

The browser worker is intentionally not deployed on Fargate. Chromium's
sandbox for untrusted sites requires the Playwright seccomp profile, while
Fargate does not accept a custom seccomp profile. Disabling
`chromium_sandbox` is not an allowed fallback.

The dedicated host downloads the Playwright v1.62.0 profile over TLS, verifies
upstream SHA-256 `cc3e61cabda6bbc1e53e54d27ba4d55a9d3be829b6dd1a596f4a7b31b1cc7849`,
and derives pinned profile SHA-256
`0c1bd13c078cd9402f43f6c471e5a52f9b49fe89505ecabf1daac01ff1124283`.
The derived profile keeps `chroot` available only after Chromium enters its
unprivileged user namespace while the outer container retains zero capabilities.
The host installs Docker `DOCKER-USER` rules and only then registers the
attested `cashcow.browser-hardened` ECS attribute.
Those rules deny private, loopback, link-local, metadata, documentation,
multicast, and reserved destinations from containers. DNS is limited to the
two exact AmazonProvidedDNS resolver addresses so the application can validate
the complete answer set; content connections remain limited to public TCP/443.
The EC2 security group has no public ingress; the runtime can reach
port 8001 only through an internal NLB.

The task definition omits `task_role_arn`, injects no secret, uses the
dedicated image/log-only execution role, runs as `pwuser`, and has
`no-new-privileges`, a read-only root filesystem, tmpfs scratch paths, and all
capabilities dropped. The application also pins validated public DNS answers,
allows only HTTPS GET/HEAD page traffic, strips credential headers, and blocks
WebSockets, downloads, service workers, sensitive inputs, and state-changing
controls. Health fails closed if Chromium, the pinned proxy, or credential
isolation is absent.

## Deployment gate

Deployment creates recurring-cost resources: one `t3.medium` EC2 instance,
one public IPv4 address, 30 GiB gp3 storage, an internal NLB, and CloudWatch
logs. Review and approve those targets before `terraform apply`. Never reuse
the `cloudmiddle` Terraform state.

Run source and container checks before obtaining AWS credentials:

```powershell
./dev/predeploy.ps1 -SkipSmoke -SkipImages

$Sha = git rev-parse HEAD
docker build --provenance=false --build-arg "RUNTIME_BUILD_SHA=$Sha" -f runtime/Dockerfile -t "cashcow-runtime:$Sha" runtime
docker build --provenance=false --build-arg "BROWSER_WORKER_BUILD_SHA=$Sha" -f browser_worker/Dockerfile -t "cashcow-browser-worker:$Sha" browser_worker
bash dev/container-smoke.sh $Sha
```

The browser smoke uses the same pinned seccomp profile, read-only rootfs,
tmpfs mounts, `pwuser`, `no-new-privileges`, and capability set as production.
It must complete a real `https://example.com/` read and verify DOM and
screenshot hashes. Import-only checks are not enough.

## Safe first deployment

Initialize the dedicated backend and inspect a refreshed plan:

```powershell
terraform -chdir=infra init -reconfigure -backend-config=backend.hcl
terraform -chdir=infra fmt -check -recursive
terraform -chdir=infra validate
terraform -chdir=infra plan
```

The plan may replace the immutable runtime task-definition revision. Terraform
will count the superseded state object as a destroy, while `skip_destroy = true`
keeps the prior ECS revision registered for rollback. The plan
must not destroy or replace the DynamoDB table, Secrets Manager secret, shared
ALB/listener, runtime target group, runtime ECS service, runtime security
group, or runtime IAM roles. Stop if any protected resource has a destructive
action.

Create only ECR first so both services always have a bootable image:

```powershell
terraform -chdir=infra apply `
  -target=aws_ecr_repository.runtime `
  -target=aws_ecr_lifecycle_policy.runtime `
  -target=aws_ecr_repository.browser_worker `
  -target=aws_ecr_lifecycle_policy.browser_worker

$Account = aws sts get-caller-identity --query Account --output text
$Registry = "$Account.dkr.ecr.ap-northeast-2.amazonaws.com"
$Sha = git rev-parse HEAD
aws ecr get-login-password --region ap-northeast-2 |
docker login --username AWS --password-stdin $Registry

docker tag "cashcow-runtime:$Sha" "${Registry}/cashcow-prod-runtime:$Sha"
docker tag "cashcow-browser-worker:$Sha" "${Registry}/cashcow-prod-browser-worker:$Sha"
docker push "${Registry}/cashcow-prod-runtime:$Sha"
docker push "${Registry}/cashcow-prod-browser-worker:$Sha"

foreach ($Repository in @("cashcow-prod-runtime", "cashcow-prod-browser-worker")) {
  aws ecr wait image-scan-complete --repository-name $Repository --image-id "imageTag=$Sha" --region ap-northeast-2
  $Critical = aws ecr describe-image-scan-findings --repository-name $Repository --image-id "imageTag=$Sha" --region ap-northeast-2 --query "imageScanFindings.findingSeverityCounts.CRITICAL" --output text
  if ($Critical -notin @("0", "None")) { throw "$Repository has $Critical critical image findings" }
}

# The new browser service needs a one-time bootstrap tag. Leave the existing
# runtime :latest unchanged; CI deploys the candidate by immutable SHA.
docker tag "cashcow-browser-worker:$Sha" "${Registry}/cashcow-prod-browser-worker:latest"
docker push "${Registry}/cashcow-prod-browser-worker:latest"

terraform -chdir=infra plan -out=cashcow.tfplan
terraform -chdir=infra show cashcow.tfplan
terraform -chdir=infra apply cashcow.tfplan
```

The full apply creates the browser log/ECR/IAM boundary, two security groups,
internal NLB/target group/listener, encrypted launch template, Auto Scaling
group, dedicated ECS cluster/capacity provider, and browser task/service. It
also tightens runtime egress and registers the runtime configuration that
points to the internal NLB. The browser service must become healthy before the
runtime service dependency completes.

## Post-deploy evidence

```powershell
$Region = "ap-northeast-2"
$BrowserCluster = "cashcow-prod-browser-worker"
$BrowserService = "cashcow-prod-browser-worker"
$RuntimeCluster = "tourmiddle-dev-cluster"
$RuntimeService = "cashcow-prod-runtime"

aws ecs wait services-stable --cluster $BrowserCluster --services $BrowserService --region $Region
aws ecs wait services-stable --cluster $RuntimeCluster --services $RuntimeService --region $Region
aws ecs describe-services --cluster $BrowserCluster --services $BrowserService --region $Region
aws elbv2 describe-target-health --target-group-arn (terraform -chdir=infra output -raw browser_worker_target_group_arn) --region $Region
Invoke-RestMethod https://d232kzujcg4ufp.cloudfront.net/cashcow/health
```

For the running browser task, require all of the following before rolling the
runtime:

- ECS health is `HEALTHY`, launch type is `EC2`, and the image digest matches
  the commit-SHA ECR tag.
- `taskRoleArn` is absent; secrets are empty; user is `pwuser`; rootfs is
  read-only; `privileged` is false; `no-new-privileges` and `drop: ALL` exist.
- The container instance has the exact
  `cashcow.browser-hardened=playwright-v1.62.0-0c1bd13c078c` attribute.
- Its EC2 metadata options require IMDSv2 and use hop limit 1.
- The internal NLB target is healthy and there is no ALB/CloudFront browser
  route.

If the worker cannot become healthy, do not disable the Chromium sandbox,
broaden egress, add a task role, or inject credentials. Leave the old runtime
revision active and inspect `/var/log/cashcow-browser-bootstrap.log` through
EC2 console diagnostics or replace the host.

## GitHub deployment

After the first Terraform apply and all evidence checks, configure:

- Secret: `AWS_ROLE_ARN` from `terraform output -raw github_actions_role_arn`
- Variables: `ENABLE_AWS_DEPLOY=true`,
  `ECR_REPOSITORY=cashcow-prod-runtime`,
  `BROWSER_WORKER_ECR_REPOSITORY=cashcow-prod-browser-worker`,
  `ECS_CLUSTER=tourmiddle-dev-cluster`,
  `ECS_SERVICE=cashcow-prod-runtime`,
  `BROWSER_WORKER_ECS_CLUSTER=cashcow-prod-browser-worker`,
  `BROWSER_WORKER_ECS_SERVICE=cashcow-prod-browser-worker`, and
  `RUNTIME_HEALTH_URL=https://d232kzujcg4ufp.cloudfront.net/cashcow/health`.

The workflow validates and smoke-tests both images before assuming the
main-only OIDC role. After push it waits for ECR scanning and refuses any image
with known critical findings. It then deploys a SHA-pinned browser task first,
verifies the host and container boundary, and finally deploys and verifies a
SHA-pinned runtime revision. `latest` is bootstrap-only; running services are
verified against the immutable SHA digest.

Only Groq and runtime service-token values belong in the existing Secrets
Manager secret. Never inject either into the browser worker or GitHub.
