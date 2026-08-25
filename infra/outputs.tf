output "ecr_repository_url" { value = aws_ecr_repository.runtime.repository_url }
output "browser_worker_ecr_repository_url" { value = aws_ecr_repository.browser_worker.repository_url }
output "ecs_service_name" { value = aws_ecs_service.runtime.name }
output "browser_worker_ecs_service_name" { value = aws_ecs_service.browser_worker.name }
output "browser_worker_ecs_cluster_name" { value = aws_ecs_cluster.browser_worker.name }
output "browser_worker_target_group_arn" { value = aws_lb_target_group.browser_worker.arn }
output "browser_worker_internal_url" {
  value       = "http://${aws_lb.browser_worker.dns_name}:${var.browser_worker_port}"
  description = "VPC-only roleless browser API behind an internal NLB; it is not exposed through ALB or CloudFront."
}
output "dynamodb_table_name" { value = aws_dynamodb_table.world.name }
output "secret_arn" {
  value     = aws_secretsmanager_secret.runtime.arn
  sensitive = true
}
output "runtime_base_url" { value = "https://d232kzujcg4ufp.cloudfront.net" }
output "github_actions_role_arn" {
  value       = aws_iam_role.github_actions.arn
  description = "GitHub Actions OIDC role for juranikr/cashcow"
}
