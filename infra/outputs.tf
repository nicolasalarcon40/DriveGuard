output "raw_trips_bucket" {
  value = aws_s3_bucket.raw_trips.bucket
}

output "risk_alerts_topic_arn" {
  value = aws_sns_topic.risk_alerts.arn
}

output "driver_risk_table" {
  value = aws_dynamodb_table.driver_risk_current.name
}

output "risk_processor_function" {
  value = aws_lambda_function.risk_processor.function_name
}
