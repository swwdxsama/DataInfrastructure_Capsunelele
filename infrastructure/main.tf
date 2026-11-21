terraform {
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.4" }
  }
}

provider "aws" {
  region = "eu-central-1"
}

variable "alert_email" {
  description = "Optional email address subscribed to the earthquake SNS alert topic."
  type        = string
  default     = ""
}

resource "aws_dynamodb_table" "sensor_events" {
  name         = "SensorEvents"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "device_id"
  range_key    = "timestamp"

  attribute {
    name = "device_id"
    type = "S"
  }
  attribute {
    name = "timestamp"
    type = "S"
  }
  attribute {
    name = "gsi_pk"
    type = "S"
  }

  global_secondary_index {
    name            = "RecentEventsIndex"
    hash_key        = "gsi_pk"
    range_key       = "timestamp"
    projection_type = "ALL"
  }
}

resource "aws_dynamodb_table" "detected_events" {
  name         = "DetectedEvents"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "zone_id"
  range_key    = "timestamp"

  attribute {
    name = "zone_id"
    type = "S"
  }
  attribute {
    name = "timestamp"
    type = "S"
  }
}

# Batch-uri brute: cheie device_id + t0 (epoch ms, numar) -> deduplicare prin attribute_not_exists
resource "aws_dynamodb_table" "raw_batches" {
  name         = "RawBatches"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "device_id"
  range_key    = "t0"

  attribute {
    name = "device_id"
    type = "S"
  }
  attribute {
    name = "t0"
    type = "N"
  }
}

resource "aws_sns_topic" "quake_alerts" {
  name = "quake-alerts-topic"
}

resource "aws_sns_topic_subscription" "email_alert" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.quake_alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# Doar codul Lambda (fara teste / __pycache__): include ingest.py, preprocess.py, process.py, correlate.py
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../src/backend"
  excludes    = ["test_*.py", "__pycache__/*", "*.pyc"]
  output_path = "${path.module}/ingest.zip"
}

resource "aws_iam_role" "lambda_exec" {
  name = "quake_lambda_exec_role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "dynamodb_access" {
  name = "quake_dynamodb_policy"
  role = aws_iam_role.lambda_exec.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
          "dynamodb:Scan"
        ]
        Resource = [
          aws_dynamodb_table.sensor_events.arn,
          "${aws_dynamodb_table.sensor_events.arn}/index/*",
          aws_dynamodb_table.detected_events.arn,
          aws_dynamodb_table.raw_batches.arn
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = aws_sns_topic.quake_alerts.arn
      }
    ]
  })
}

resource "aws_lambda_function" "ingest" {
  function_name    = "quake-telemetry-ingest"
  role             = aws_iam_role.lambda_exec.arn
  handler          = "ingest.lambda_handler"
  runtime          = "python3.12"
  timeout          = 15
  memory_size      = 512
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  environment {
    variables = merge(local.pipeline_env, {
      TABLE_NAME            = aws_dynamodb_table.sensor_events.name
      ALERTS_TABLE_NAME     = aws_dynamodb_table.detected_events.name
      RAW_TABLE_NAME        = aws_dynamodb_table.raw_batches.name
      SNS_TOPIC_ARN         = aws_sns_topic.quake_alerts.arn
      PREPROCESS_ENABLED    = var.preprocess_enabled ? "1" : "0"
      SPIKE_ACC_THRESHOLD   = "2.5"
      STATIONARY_GYRO_RMS   = "1.0"
      STA_LTA_RATIO         = "3.0"
      LTA_SEC               = "30"
      WARMUP_SEC            = "15"
      STATIONARY_WINDOW_SEC = "10"
      ZONE_RADIUS_KM        = "5.0"
      QUORUM_WINDOW_SEC     = "15"
      QUORUM_MIN_DEVICES    = "2"
      QUORUM_MIN_RATIO      = "0.5"
      ALERT_COOLDOWN_SEC    = "30"
    })
  }
}

resource "aws_apigatewayv2_api" "http_api" {
  name          = "quake-telemetry-api"
  protocol_type = "HTTP"
  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["Content-Type", "x-api-key"]
  }
}

resource "aws_apigatewayv2_integration" "lambda_integration" {
  api_id                 = aws_apigatewayv2_api.http_api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.ingest.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "post_telemetry" {
  api_id    = aws_apigatewayv2_api.http_api.id
  route_key = "POST /telemetry"
  target    = "integrations/${aws_apigatewayv2_integration.lambda_integration.id}"
}

resource "aws_apigatewayv2_route" "get_events" {
  api_id    = aws_apigatewayv2_api.http_api.id
  route_key = "GET /events"
  target    = "integrations/${aws_apigatewayv2_integration.lambda_integration.id}"
}

resource "aws_apigatewayv2_route" "get_alerts" {
  api_id    = aws_apigatewayv2_api.http_api.id
  route_key = "GET /alerts"
  target    = "integrations/${aws_apigatewayv2_integration.lambda_integration.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http_api.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "api_gw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingest.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http_api.execution_arn}/*/*"
}

output "raw_batches_table" {
  value = aws_dynamodb_table.raw_batches.name
}

output "api_url" {
  value = aws_apigatewayv2_api.http_api.api_endpoint
}
