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
  input.action == "approval.decide"
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
