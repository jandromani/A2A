# The application can read only its runtime credentials. In particular, it
# cannot read the database migration credential or provisioned agent secrets.
path "secret/data/a2a/production/database" {
  capabilities = ["read"]
}

path "secret/data/a2a/production/valkey" {
  capabilities = ["read"]
}

path "secret/data/a2a/production/providers/*" {
  capabilities = ["read"]
}

path "secret/data/a2a/production/observability/langfuse" {
  capabilities = ["read"]
}

path "secret/data/a2a/production/integrations/*" {
  capabilities = ["read"]
}
