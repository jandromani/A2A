# Public evidence-to-claim matrix

| Claim | What is publicly inspectable | Scope of proof | Not established |
| --- | --- | --- | --- |
| Three end-to-end reference workflows | `app/workflows`, `tests/test_platform.py` | Local or fixture-backed scenarios | Universal workflow runtime |
| Permissioned human-review writes | `app/policy.py`, `app/jira.py`, `tests/test_governance.py` | Auth logic and mocked adapters | Production IAM rollout |
| MCP/A2A/Agora integration | `app/sdk_protocols.py`, `app/protocols.py`, `tests/test_protocol_interop.py` | Selected contracts & local tests | Multi-vendor gateway E2E |
| Persistent data, outbox, stream workers | `app/database.py`, `app/event_worker.py`, Compose | Implementation & fixtures | Resilience under measured enterprise load |
| OIDC, Vault, OPA, archived audit | Preflight, config, archive tests | Implementation and fail-closed requirements | Secrets, realm/policies and S3 Object Lock installed in production |
| Engineering velocity | Commit history 13–14 October 2026 | Short public code-assembly window | Productivity uplift vs a controlled baseline |
| AI-assisted work | Engineering process disclosure in README | Self-described method | Per-change agent transcript, human acceptance ratio or signed review trail |
| CI status | [Actions](https://github.com/jandromani/A2A/actions/workflows/ci.yml) | Actual commit-specific execution | Operational SLOs or customer production readiness |

**Release policy:** a claim is only elevated when its proof, environmental assumptions, actual CI SHA and independent validation are published. Shipping code on two calendar days is a method demonstration, not a production uptime record.
