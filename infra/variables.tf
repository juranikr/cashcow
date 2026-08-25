variable "aws_region" {
  type    = string
  default = "ap-northeast-2"
}

variable "name_prefix" {
  type    = string
  default = "cashcow-prod"
}

variable "shared_cluster_name" {
  type    = string
  default = "tourmiddle-dev-cluster"
}

variable "shared_alb_name" {
  type    = string
  default = "tourmiddle-dev-alb"
}

variable "shared_alb_security_group_id" {
  type    = string
  default = "sg-08ff7c5ceae900541"
}

variable "vpc_id" {
  type    = string
  default = "vpc-0c1828bb31502023e"
}

variable "public_subnet_ids" {
  type    = list(string)
  default = ["subnet-00db04d04efbf094c", "subnet-099b1fa1d5bd71b89"]
}

variable "container_port" {
  type    = number
  default = 8000
}

variable "browser_worker_port" {
  type    = number
  default = 8001
}

variable "browser_worker_instance_type" {
  type        = string
  default     = "t3.medium"
  description = "Dedicated ECS EC2 host size for the sandboxed Chromium worker."
}

variable "listener_rule_priority" {
  type    = number
  default = 210
}

variable "github_org" {
  type    = string
  default = "juranikr"
}

variable "github_repo" {
  type    = string
  default = "cashcow"
}

variable "github_org_id" {
  type        = string
  default     = "295397696"
  description = "Immutable GitHub owner ID used in the OIDC subject claim."
}

variable "github_repo_id" {
  type        = string
  default     = "1343838593"
  description = "Immutable GitHub repository ID used in the OIDC subject claim."
}
