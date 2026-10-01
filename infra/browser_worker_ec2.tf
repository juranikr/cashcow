locals {
  browser_worker_cluster_name        = "${var.name_prefix}-browser-worker"
  playwright_seccomp_url             = "https://raw.githubusercontent.com/microsoft/playwright/v1.62.0/utils/docker/seccomp_profile.json"
  playwright_seccomp_upstream_sha256 = "cc3e61cabda6bbc1e53e54d27ba4d55a9d3be829b6dd1a596f4a7b31b1cc7849"
  playwright_seccomp_derived_sha256  = "0c1bd13c078cd9402f43f6c471e5a52f9b49fe89505ecabf1daac01ff1124283"
  browser_host_attestation           = "playwright-v1.62.0-${substr(local.playwright_seccomp_derived_sha256, 0, 12)}"
}

data "aws_ssm_parameter" "ecs_al2023_ami" {
  name = "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id"
}

data "aws_vpc" "selected" {
  id = var.vpc_id
}

# Fargate cannot apply the custom seccomp profile required by Chromium's
# sandbox. This dedicated EC2 cluster registers a host only after the pinned
# Playwright profile, user namespaces, and Docker egress firewall are active.
resource "aws_ecs_cluster" "browser_worker" {
  name = local.browser_worker_cluster_name

  setting {
    name  = "containerInsights"
    value = var.hibernated ? "disabled" : "enabled"
  }

  tags = { Project = "cashcow", Environment = "prod", Boundary = "public-read-only" }
}

resource "aws_iam_role" "browser_instance" {
  name = "${var.name_prefix}-browser-host"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Project = "cashcow", Environment = "prod", Boundary = "public-read-only" }
}

resource "aws_iam_role_policy_attachment" "browser_instance_ecs" {
  role       = aws_iam_role.browser_instance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_instance_profile" "browser_worker" {
  name = "${var.name_prefix}-browser-host"
  role = aws_iam_role.browser_instance.name
}

resource "aws_security_group" "browser_worker_host" {
  name        = "${var.name_prefix}-browser-host"
  description = "Dedicated hardened ECS host for the public read-only browser"
  vpc_id      = var.vpc_id

  tags = { Project = "cashcow", Environment = "prod", Boundary = "public-read-only" }
}

resource "aws_security_group" "browser_worker_nlb" {
  name        = "${var.name_prefix}-browser-nlb"
  description = "Internal browser API reachable from Cashcow runtime only"
  vpc_id      = var.vpc_id

  tags = { Project = "cashcow", Environment = "prod", Boundary = "internal-only" }
}

resource "aws_vpc_security_group_ingress_rule" "browser_nlb_from_runtime" {
  security_group_id            = aws_security_group.browser_worker_nlb.id
  referenced_security_group_id = aws_security_group.runtime.id
  description                  = "Browser API from Cashcow runtime only"
  ip_protocol                  = "tcp"
  from_port                    = var.browser_worker_port
  to_port                      = var.browser_worker_port
}

resource "aws_vpc_security_group_egress_rule" "browser_nlb_to_host" {
  security_group_id            = aws_security_group.browser_worker_nlb.id
  referenced_security_group_id = aws_security_group.browser_worker_host.id
  description                  = "NLB listener and health checks to browser host"
  ip_protocol                  = "tcp"
  from_port                    = var.browser_worker_port
  to_port                      = var.browser_worker_port
}

resource "aws_vpc_security_group_ingress_rule" "browser_host_from_nlb" {
  security_group_id            = aws_security_group.browser_worker_host.id
  referenced_security_group_id = aws_security_group.browser_worker_nlb.id
  description                  = "Browser container from internal NLB only"
  ip_protocol                  = "tcp"
  from_port                    = var.browser_worker_port
  to_port                      = var.browser_worker_port
}

resource "aws_vpc_security_group_egress_rule" "browser_host_public_https" {
  security_group_id = aws_security_group.browser_worker_host.id
  description       = "ECS control plane, image pull, logs, and public HTTPS reads"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_launch_template" "browser_worker" {
  name_prefix            = "${var.name_prefix}-browser-"
  image_id               = data.aws_ssm_parameter.ecs_al2023_ami.value
  instance_type          = var.browser_worker_instance_type
  update_default_version = true

  iam_instance_profile { name = aws_iam_instance_profile.browser_worker.name }

  network_interfaces {
    associate_public_ip_address = true
    delete_on_termination       = true
    security_groups             = [aws_security_group.browser_worker_host.id]
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_protocol_ipv6          = "disabled"
    http_put_response_hop_limit = 1
    http_tokens                 = "required"
    instance_metadata_tags      = "disabled"
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      delete_on_termination = true
      encrypted             = true
      volume_size           = 30
      volume_type           = "gp3"
    }
  }

  user_data = base64encode(templatefile("${path.module}/browser-host-user-data.sh.tftpl", {
    browser_worker_cluster_name = aws_ecs_cluster.browser_worker.name
    browser_worker_port         = var.browser_worker_port
    host_attestation            = local.browser_host_attestation
    seccomp_deriver_base64      = filebase64("${path.module}/../dev/derive-seccomp.py")
    seccomp_derived_sha256      = local.playwright_seccomp_derived_sha256
    seccomp_upstream_sha256     = local.playwright_seccomp_upstream_sha256
    seccomp_url                 = local.playwright_seccomp_url
    # Public-destination validation needs DNS, but the container may talk only
    # to AmazonProvidedDNS rather than arbitrary port 53 endpoints.
    vpc_dns_resolvers = join(" ", [
      cidrhost(data.aws_vpc.selected.cidr_block, 2),
      "169.254.169.253",
    ])
    vpc_cidrs = join(" ", distinct(concat(
      [data.aws_vpc.selected.cidr_block],
      [for association in data.aws_vpc.selected.cidr_block_associations : association.cidr_block],
    )))
  }))

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name        = "${var.name_prefix}-browser-host"
      Project     = "cashcow"
      Environment = "prod"
      Boundary    = "public-read-only"
    }
  }

  tag_specifications {
    resource_type = "volume"
    tags          = { Project = "cashcow", Environment = "prod", Boundary = "public-read-only" }
  }

  lifecycle { create_before_destroy = true }
}

resource "aws_autoscaling_group" "browser_worker" {
  name_prefix         = "${var.name_prefix}-browser-"
  min_size            = var.hibernated ? 0 : 1
  max_size            = var.hibernated ? 0 : 2
  desired_capacity    = var.hibernated ? 0 : 1
  vpc_zone_identifier = var.public_subnet_ids
  health_check_type   = "EC2"

  launch_template {
    id      = aws_launch_template.browser_worker.id
    version = "$Latest"
  }

  instance_refresh {
    strategy = "Rolling"
    preferences {
      instance_warmup        = 180
      min_healthy_percentage = 0
      max_healthy_percentage = 200
    }
  }

  tag {
    key                 = "AmazonECSManaged"
    value               = "true"
    propagate_at_launch = true
  }

  tag {
    key                 = "Project"
    value               = "cashcow"
    propagate_at_launch = true
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_ecs_capacity_provider" "browser_worker" {
  name = "${var.name_prefix}-browser-worker"

  auto_scaling_group_provider {
    auto_scaling_group_arn         = aws_autoscaling_group.browser_worker.arn
    managed_termination_protection = "DISABLED"

    managed_scaling {
      status                    = "ENABLED"
      target_capacity           = 100
      minimum_scaling_step_size = 1
      maximum_scaling_step_size = 1
      instance_warmup_period    = 180
    }
  }

  tags = { Project = "cashcow", Environment = "prod", Boundary = "public-read-only" }
}

resource "aws_ecs_cluster_capacity_providers" "browser_worker" {
  cluster_name       = aws_ecs_cluster.browser_worker.name
  capacity_providers = [aws_ecs_capacity_provider.browser_worker.name]

  default_capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.browser_worker.name
    base              = 1
    weight            = 100
  }
}

resource "aws_lb" "browser_worker" {
  count = var.hibernated ? 0 : 1

  name                             = "${var.name_prefix}-browser-nlb"
  internal                         = true
  load_balancer_type               = "network"
  subnets                          = var.public_subnet_ids
  security_groups                  = [aws_security_group.browser_worker_nlb.id]
  enable_cross_zone_load_balancing = true
  enable_deletion_protection       = true

  tags = { Project = "cashcow", Environment = "prod", Boundary = "internal-only" }
}

resource "aws_lb_target_group" "browser_worker" {
  count = var.hibernated ? 0 : 1

  name        = "${var.name_prefix}-browser"
  port        = var.browser_worker_port
  protocol    = "TCP"
  target_type = "instance"
  vpc_id      = var.vpc_id

  deregistration_delay = 15

  health_check {
    enabled             = true
    protocol            = "HTTP"
    path                = "/health"
    matcher             = "200"
    interval            = 20
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }

  tags = { Project = "cashcow", Environment = "prod", Boundary = "internal-only" }
}

resource "aws_lb_listener" "browser_worker" {
  count = var.hibernated ? 0 : 1

  load_balancer_arn = aws_lb.browser_worker[0].arn
  port              = var.browser_worker_port
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.browser_worker[0].arn
  }
}

resource "aws_ecs_task_definition" "browser_worker" {
  family                   = "${var.name_prefix}-browser-worker"
  requires_compatibilities = ["EC2"]
  network_mode             = "bridge"
  cpu                      = "1024"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.browser_execution.arn

  placement_constraints {
    type       = "memberOf"
    expression = "attribute:cashcow.browser-hardened == ${local.browser_host_attestation}"
  }

  # Deliberately omit task_role_arn. The browser must never receive AWS task
  # credentials or application secrets.
  container_definitions = jsonencode([{
    name                   = "browser-worker"
    image                  = "${aws_ecr_repository.browser_worker.repository_url}:latest"
    essential              = true
    user                   = "pwuser"
    readonlyRootFilesystem = true
    privileged             = false
    dockerSecurityOptions  = ["no-new-privileges"]
    environment            = []
    portMappings           = [{ containerPort = var.browser_worker_port, hostPort = var.browser_worker_port, protocol = "tcp" }]
    linuxParameters = {
      initProcessEnabled = true
      sharedMemorySize   = 512
      capabilities       = { add = [], drop = ["ALL"] }
      tmpfs = [
        { containerPath = "/tmp", size = 512, mountOptions = ["rw", "nosuid", "nodev"] },
        { containerPath = "/home/pwuser", size = 128, mountOptions = ["rw", "noexec", "nosuid", "nodev", "uid=1001", "gid=1001", "mode=0700"] },
      ]
    }
    mountPoints    = []
    systemControls = []
    volumesFrom    = []
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.browser_worker.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "browser-worker"
      }
    }
    healthCheck = {
      command = [
        "CMD-SHELL",
        "python -c \"import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:${var.browser_worker_port}/health')); assert d['status']=='ok' and d['buildSha']!='unknown' and d.get('credentialIsolation') and not d.get('forbiddenCredentialEnvironmentPresent') and not d.get('awsCredentialsPresent') and not d.get('groqKeyPresent') and d.get('networkProxy') and d.get('pinnedHttpsProxy') and d.get('safeMethodsOnly') and d.get('chromiumSandboxRequired') and d.get('publicHttpsOnly')\" || exit 1",
      ]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }
  }])

  depends_on = [aws_iam_role_policy.browser_execution]
  tags       = { Project = "cashcow", Environment = "prod", Boundary = "public-read-only" }
  # CI promotes immutable revisions. Preserve prior hardened definitions for
  # deployment-circuit-breaker and manual rollback targets.
  skip_destroy = true

  lifecycle { create_before_destroy = true }
}

resource "aws_ecs_service" "browser_worker" {
  name                  = "${var.name_prefix}-browser-worker"
  cluster               = aws_ecs_cluster.browser_worker.arn
  task_definition       = aws_ecs_task_definition.browser_worker.arn
  desired_count         = var.hibernated ? 0 : 1
  wait_for_steady_state = true

  capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.browser_worker.name
    base              = 1
    weight            = 100
  }

  placement_constraints {
    type       = "memberOf"
    expression = "attribute:cashcow.browser-hardened == ${local.browser_host_attestation}"
  }

  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  dynamic "load_balancer" {
    for_each = var.hibernated ? [] : [1]

    content {
      target_group_arn = aws_lb_target_group.browser_worker[0].arn
      container_name   = "browser-worker"
      container_port   = var.browser_worker_port
    }
  }

  depends_on = [
    aws_ecs_cluster_capacity_providers.browser_worker,
    aws_lb_listener.browser_worker,
  ]

  tags = { Project = "cashcow", Environment = "prod", Boundary = "public-read-only" }

  lifecycle { ignore_changes = [task_definition] }
}
