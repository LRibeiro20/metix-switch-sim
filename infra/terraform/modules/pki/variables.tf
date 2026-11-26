variable "ca_validity_hours" {
  description = "Validity of the Root CA certificate in hours."
  type        = number
}

variable "certs_validity_hours" {
  description = "Validity of each service certificate in hours."
  type        = number
}
