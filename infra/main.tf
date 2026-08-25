data "aws_ecs_cluster" "shared" {
  cluster_name = var.shared_cluster_name
}

data "aws_lb" "shared" {
  name = var.shared_alb_name
}

data "aws_lb_listener" "shared_http" {
  load_balancer_arn = data.aws_lb.shared.arn
  port              = 80
}

resource "aws_ecr_repository" "runtime" {
  name                 = "${var.name_prefix}-runtime"
  image_tag_mutability = "IMMUTABLE_WITH_EXCLUSION"

  image_tag_mutability_exclusion_filter {
    filter      = "latest"
    filter_type = "WILDCARD"
  }

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Project = "cashcow", Environment = "prod" }
}

resource "aws_ecr_lifecycle_policy" "runtime" {
  repository = aws_ecr_repository.runtime.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the latest 12 runtime images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 12
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_ecr_repository" "browser_worker" {
  name                 = "${var.name_prefix}-browser-worker"
  image_tag_mutability = "IMMUTABLE_WITH_EXCLUSION"

  image_tag_mutability_exclusion_filter {
    filter      = "latest"
    filter_type = "WILDCARD"
  }

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Project = "cashcow", Environment = "prod", Boundary = "public-read-only" }
}

resource "aws_ecr_lifecycle_policy" "browser_worker" {
  repository = aws_ecr_repository.browser_worker.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the latest 12 browser worker images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 12
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_dynamodb_table" "world" {
  name                        = "${var.name_prefix}-world"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "pk"
  range_key                   = "sk"
  deletion_protection_enabled = true

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery { enabled = true }

  tags = { Project = "cashcow", Environment = "prod" }
}

resource "aws_secretsmanager_secret" "runtime" {
  name                    = "${var.name_prefix}-runtime"
  recovery_window_in_days = 7
  tags                    = { Project = "cashcow", Environment = "prod" }
}

resource "aws_cloudwatch_log_group" "runtime" {
  name              = "/ecs/${var.name_prefix}-runtime"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "browser_worker" {
  name              = "/ecs/${var.name_prefix}-browser-worker"
  retention_in_days = 30
  tags              = { Project = "cashcow", Environment = "prod", Boundary = "public-read-only" }
}

resource "aws_iam_role" "execution" {
  name = "${var.name_prefix}-runtime-exec"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "browser_execution" {
  name = "${var.name_prefix}-browser-worker-exec"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Project = "cashcow", Environment = "prod", Boundary = "public-read-only" }
}

resource "aws_iam_role_policy" "browser_execution" {
  name = "${var.name_prefix}-browser-worker-exec"
  role = aws_iam_role.browser_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrAuthorization"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid    = "PullBrowserWorkerImage"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
        ]
        Resource = aws_ecr_repository.browser_worker.arn
      },
      {
        Sid    = "WriteBrowserWorkerLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "${aws_cloudwatch_log_group.browser_worker.arn}:*"
      },
    ]
  })
}

resource "aws_iam_role_policy" "execution_secret" {
  name = "${var.name_prefix}-runtime-secret"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.runtime.arn }]
  })
}

resource "aws_iam_role" "task" {
  name = "${var.name_prefix}-runtime-task"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "task_dynamodb" {
  name = "${var.name_prefix}-world"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "dynamodb:Scan",
        "dynamodb:TransactWriteItems",
      ]
      Resource = aws_dynamodb_table.world.arn
    }]
  })
}

resource "aws_security_group" "runtime" {
  name        = "${var.name_prefix}-runtime"
  description = "Cashcow runtime from shared ALB only"
  vpc_id      = var.vpc_id

  ingress {
    description     = "Runtime API from shared ALB"
    from_port       = var.container_port
    to_port         = var.container_port
    protocol        = "tcp"
    security_groups = [var.shared_alb_security_group_id]
  }

  egress {
    description = "HTTPS APIs only"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description     = "Runtime to isolated browser API only"
    from_port       = var.browser_worker_port
    to_port         = var.browser_worker_port
    protocol        = "tcp"
    security_groups = [aws_security_group.browser_worker_nlb.id]
  }

  tags = { Project = "cashcow", Environment = "prod" }
}

resource "aws_lb_target_group" "runtime" {
  name        = "${var.name_prefix}-runtime"
  port        = var.container_port
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id

  deregistration_delay = 10

  health_check {
    enabled             = true
    path                = "/cashcow/health"
    matcher             = "200"
    interval            = 20
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = { Project = "cashcow", Environment = "prod" }
}

resource "aws_lb_listener_rule" "runtime" {
  listener_arn = data.aws_lb_listener.shared_http.arn
  priority     = var.listener_rule_priority

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.runtime.arn
  }

  condition {
    path_pattern { values = ["/cashcow", "/cashcow/*"] }
  }

  tags = { Project = "cashcow", Environment = "prod" }
}

resource "aws_ecs_task_definition" "runtime" {
  family                   = "${var.name_prefix}-runtime"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name                   = "runtime"
    image                  = "${aws_ecr_repository.runtime.repository_url}:latest"
    essential              = true
    readonlyRootFilesystem = true
    portMappings           = [{ containerPort = var.container_port, hostPort = var.container_port, protocol = "tcp" }]
    environment = [
      { name = "AWS_REGION", value = var.aws_region },
      { name = "DYNAMODB_TABLE", value = aws_dynamodb_table.world.name },
      { name = "BROWSER_WORKER_URL", value = "http://${aws_lb.browser_worker.dns_name}:${var.browser_worker_port}" },
      { name = "GROQ_MODEL", value = "openai/gpt-oss-120b" },
      { name = "GROQ_STRUCTURED_MODEL", value = "openai/gpt-oss-120b" },
    ]
    secrets = [
      { name = "GROQ_API_KEY", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:GROQ_API_KEY::" },
      { name = "RUNTIME_SERVICE_TOKEN", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:RUNTIME_SERVICE_TOKEN::" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.runtime.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "runtime"
      }
    }
    linuxParameters = {
      initProcessEnabled = true
      capabilities       = { drop = ["ALL"] }
    }
    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/cashcow/health')\" || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 25
    }
  }])

  depends_on = [aws_iam_role_policy_attachment.execution, aws_iam_role_policy.execution_secret]
  tags       = { Project = "cashcow", Environment = "prod" }
  # CI promotes immutable revisions. Keep older revisions registered so an ECS
  # circuit-breaker rollback (or an operator rollback) always has a valid target.
  skip_destroy = true

  lifecycle { create_before_destroy = true }
}

resource "aws_ecs_service" "runtime" {
  name                  = "${var.name_prefix}-runtime"
  cluster               = data.aws_ecs_cluster.shared.arn
  task_definition       = aws_ecs_task_definition.runtime.arn
  desired_count         = 1
  launch_type           = "FARGATE"
  wait_for_steady_state = true

  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.public_subnet_ids
    security_groups  = [aws_security_group.runtime.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.runtime.arn
    container_name   = "runtime"
    container_port   = var.container_port
  }

  depends_on = [aws_lb_listener_rule.runtime, aws_ecs_service.browser_worker]
  tags       = { Project = "cashcow", Environment = "prod" }

  lifecycle { ignore_changes = [task_definition] }
}
