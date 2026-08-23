output "ecr_repository_url" { value = aws_ecr_repository.runtime.repository_url }
output "ecs_service_name" { value = aws_ecs_service.runtime.name }
output "dynamodb_table_name" { value = aws_dynamodb_table.world.name }
output "secret_arn" {
  value     = aws_secretsmanager_secret.runtime.arn
  sensitive = true
}
output "runtime_base_url" { value = "https://d232kzujcg4ufp.cloudfront.net" }
