variable "namespace" {
  description = "Namespace to create for the METIX switch stack."
  type        = string
}

variable "pki_bundle" {
  description = "Map of file name => PEM content mounted read-only at /certs."
  type        = map(string)
  sensitive   = true
}

variable "databases" {
  description = "Credentials for the switch, bci and stdb PostgreSQL databases."
  type = map(object({
    user     = string
    password = string
    db       = string
  }))
  sensitive = true
}
