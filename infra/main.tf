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
  image_tag_mutability = "MUTABLE"

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
        "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
        "dynamodb:Query", "dynamodb:Scan", "dynamodb:BatchGetItem", "dynamodb:BatchWriteItem"
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
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
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
    name         = "runtime"
    image        = "${aws_ecr_repository.runtime.repository_url}:latest"
    essential    = true
    portMappings = [{ containerPort = var.container_port, hostPort = var.container_port, protocol = "tcp" }]
    environment = [
      { name = "AWS_REGION", value = var.aws_region },
      { name = "DYNAMODB_TABLE", value = aws_dynamodb_table.world.name },
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
}

resource "aws_ecs_service" "runtime" {
  name            = "${var.name_prefix}-runtime"
  cluster         = data.aws_ecs_cluster.shared.arn
  task_definition = aws_ecs_task_definition.runtime.arn
  desired_count   = 1
  launch_type     = "FARGATE"

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

  depends_on = [aws_lb_listener_rule.runtime]
  tags       = { Project = "cashcow", Environment = "prod" }
}
