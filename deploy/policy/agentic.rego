package agentic

default allow := false

allow if {
  input.action == "task.create"
  some role in input.actor.roles
  role in {"employee", "operator", "admin"}
}

allow if {
  input.action == "task.cancel"
  some role in input.actor.roles
  role in {"operator", "admin"}
}

allow if {
  input.action == "knowledge.read"
  some role in input.actor.roles
  role in {"employee", "operator", "approver", "admin"}
}

allow if {
  input.action == "knowledge.write"
  "admin" in input.actor.roles
}

allow if {
  input.action == "catalog.manage"
  "admin" in input.actor.roles
}

allow if {
  input.action == "connector.invoke"
  some role in input.actor.roles
  role in {"approver", "admin"}
}

allow if {
  input.action == "connector.read"
  some role in input.actor.roles
  role in {"operator", "approver", "admin"}
}

allow if {
  input.action == "approval.decide"
  some role in input.actor.roles
  role in {"approver", "admin"}
}

allow if {
  input.action == "approval.read"
  some role in input.actor.roles
  role in {"approver", "admin"}
}

allow if {
  input.action == "audit.read"
  some role in input.actor.roles
  role in {"operator", "approver", "admin"}
}

allow if {
  input.action == "evaluation.read"
  some role in input.actor.roles
  role in {"operator", "approver", "admin"}
}

allow if {
  input.action == "usage.read"
  some role in input.actor.roles
  role in {"operator", "approver", "admin"}
}

allow if {
  input.action == "data.source.manage"
  "admin" in input.actor.roles
}

allow if {
  input.action == "data.source.read"
  some role in input.actor.roles
  role in {"operator", "approver", "admin"}
}

allow if {
  input.action in {"data.source.sync", "data.source.write"}
  some role in input.actor.roles
  role in {"operator", "admin"}
  scope_allowed
}

allow if {
  input.action == "workflow.manage"
  "admin" in input.actor.roles
}

allow if {
  input.action == "identity.manage"
  "admin" in input.actor.roles
}

allow if {
  input.action == "workflow.run"
  some role in input.actor.roles
  role in {"employee", "operator", "admin"}
}

allow if {
  input.action in {"integration.github.read", "integration.github.request_write"}
  some role in input.actor.roles
  role in {"employee", "operator", "approver", "admin"}
  scope_allowed
}

allow if {
  input.action in {"integration.gmail.read", "integration.gmail.request_send"}
  some role in input.actor.roles
  role in {"employee", "operator", "approver", "admin"}
  scope_allowed
}

scope_allowed if "*" in input.actor.scopes
scope_allowed if input.resource.scope in input.actor.scopes
