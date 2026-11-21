# Praguri pipeline (preprocesare / trigger / corelare) -> variabile de mediu Lambda.
# Valorile implicite sunt cele pentru demo.

variable "preprocess_enabled" {
  description = "Ruleaza preprocess in handler (activeaza dupa ce App.tsx trimite contractul v1)"
  type        = bool
  default     = false
}

variable "pipeline" {
  description = "Praguri pipeline"
  type = object({
    MIN_BATCH_SEC         = number
    MAX_BATCH_AGE_SEC     = number
    MAX_GAP_FRACTION      = number
    STA_SEC               = number
    LTA_SEC               = number
    STA_LTA_RATIO         = number
    SPIKE_ACC_THRESHOLD   = number
    STATIONARY_GYRO_RMS   = number
    STATIONARY_WINDOW_SEC = number
    ZONE_RADIUS_KM        = number
    QUORUM_WINDOW_SEC     = number
    QUORUM_MIN_DEVICES    = number
    QUORUM_MIN_RATIO      = number
    ACTIVE_WINDOW_SEC     = number
  })
  default = {
    # Preprocesare
    MIN_BATCH_SEC     = 1
    MAX_BATCH_AGE_SEC = 60
    MAX_GAP_FRACTION  = 0.2
    # Trigger
    STA_SEC               = 1
    LTA_SEC               = 30
    STA_LTA_RATIO         = 3.0
    SPIKE_ACC_THRESHOLD   = 2.5
    STATIONARY_GYRO_RMS   = 1.0
    STATIONARY_WINDOW_SEC = 10
    # Corelare
    ZONE_RADIUS_KM     = 5.0
    QUORUM_WINDOW_SEC  = 15
    QUORUM_MIN_DEVICES = 2
    QUORUM_MIN_RATIO   = 0.5
    ACTIVE_WINDOW_SEC  = 30
  }
}

locals {
  pipeline_env = { for k, v in var.pipeline : k => tostring(v) }
}
