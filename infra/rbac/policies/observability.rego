# PP31 (T3.7.2): declarative RBAC for the observability platform.
#
# Three rules from the ticket:
#   1. deny if user.tenant_id != resource.tenant_id   (cross-tenant access)
#   2. deny if action requires MFA and user.mfa_verified is not true
#   3. allow only when the user's role holds the requested action
#
# role_permissions mirrors agent_obs/rbac/roles.py ROLES (PP30 model, including
# the documented compressed:expand deviation). Keep the two in sync: the Python
# side is guarded by validate_roles(), this file by
# tests/test_policy_engine.py::TestRegoPolicy (runs `opa eval`, skips when the
# opa binary is not installed).
#
# Queried by agent_obs/rbac/policy_engine.py via the OPA Data API:
#   POST /v1/data/observability/rbac/allow  {"input": {...}}

package observability.rbac

import rego.v1

# Fail-closed: anything the rules below do not explicitly allow is denied
# (unknown role, missing fields, undefined input — all evaluate to false).
default allow := false

# Canonical role -> permissions (PP30 model). Mirrors ROLES in
# agent_obs/rbac/roles.py; see that module's docstring for the PP19 deviation.
role_permissions := {
	"admin": {"trace:read", "pii:recover", "policy:write", "config:write", "deploy:gate", "audit:read", "slo:write", "compressed:expand"},
	"sre": {"trace:read", "pii:recover", "config:write", "deploy:gate", "audit:read", "slo:read", "compressed:expand"},
	"developer": {"trace:read", "audit:read", "slo:read", "compressed:expand"},
	"dpo": {"trace:read", "audit:read", "compliance:export"},
	"auditor": {"audit:read", "compliance:export"},
	"agent": {"trace:write", "span:write", "metrics:write"},
}

# Actions that require a verified MFA challenge (ticket list). "audit:read:raw"
# is not a role permission in the PP30 model — it is kept here so a raw-audit
# action can never pass without MFA even if granted out-of-band later.
mfa_actions := {"pii:recover", "audit:read:raw"}

requires_mfa(action) if mfa_actions[action]

# Role grants the action, tenants match, action needs no MFA.
allow if {
	# Key lookup doubles as the role-existence check: an unknown role makes
	# the lookup undefined -> the rule fails -> deny (fail-closed).
	# NOTE: `role in role_permissions` would test the object's *values*, not
	# its keys — that form is deliberately avoided here.
	permissions := role_permissions[input.user.role]
	input.action in permissions
	input.user.tenant_id == input.resource.tenant_id
	not requires_mfa(input.action)
}

# Role grants the action, tenants match, action needs MFA and MFA is verified.
allow if {
	permissions := role_permissions[input.user.role]
	input.action in permissions
	input.user.tenant_id == input.resource.tenant_id
	requires_mfa(input.action)
	input.user.mfa_verified == true
}
