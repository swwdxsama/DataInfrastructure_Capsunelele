# Automated emergency dispatch voice call triggered by SNS quake-alerts-topic.

variable "dispatch_provider" {
  description = "Voice call provider: 'twilio' (default, works with +40 trial) or 'aws' (End User Messaging Voice)."
  type        = string
  default     = "twilio"
}

variable "dispatch_to_number" {
  description = "Destination E.164 phone number to call on earthquake alert (e.g. +40712345678). Empty disables calls."
  type        = string
  default     = ""
  sensitive   = true
}

variable "dispatch_from_number" {
  description = "Caller ID / origination phone number in E.164 format (Twilio number or AWS Voice origination number)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "dispatch_dry_run" {
  description = "If true, validates dispatch call formatting without placing an actual phone call."
  type        = bool
  default     = false
}

variable "twilio_account_sid" {
  description = "Twilio Account SID (only used when dispatch_provider = 'twilio')."
  type        = string
  default     = ""
  sensitive   = true
}

variable "twilio_auth_token" {
  description = "Twilio Auth Token (only used when dispatch_provider = 'twilio')."
  type        = string
  default     = ""
  sensitive   = true
}

resource "aws_iam_role_policy" "dispatch_voice" {
  name = "quake_dispatch_voice_policy"
  role = aws_iam_role.lambda_exec.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sms-voice:SendVoiceMessage"]
        Resource = "*"
      }
    ]
  })
}

resource "aws_lambda_function" "dispatch" {
  function_name    = "quake-dispatch-call"
  role             = aws_iam_role.lambda_exec.arn
  handler          = "dispatch.lambda_handler"
  runtime          = "python3.12"
  timeout          = 30
  memory_size      = 256
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  environment {
    variables = {
      DISPATCH_PROVIDER    = var.dispatch_provider
      DISPATCH_TO_NUMBER   = var.dispatch_to_number
      DISPATCH_FROM_NUMBER = var.dispatch_from_number
      DISPATCH_DRY_RUN     = var.dispatch_dry_run ? "1" : "0"
      TWILIO_ACCOUNT_SID   = var.twilio_account_sid
      TWILIO_AUTH_TOKEN    = var.twilio_auth_token
    }
  }
}

resource "aws_sns_topic_subscription" "dispatch_lambda" {
  topic_arn = aws_sns_topic.quake_alerts.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.dispatch.arn
}

resource "aws_lambda_permission" "sns_dispatch" {
  statement_id  = "AllowSNSInvokeDispatch"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.dispatch.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.quake_alerts.arn
}
