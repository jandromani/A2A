# A2A Tools SDK

Versioned Python package for defining validated, permission-aware tools and exposing them to an MCP server. It has no dependency on the platform application and can be installed on its own:

```bash
pip install '.[mcp]'
```

```python
from pydantic import BaseModel
from mcp.server import MCPServer
from a2a_tools_sdk import ToolContext, ToolRegistry

class LookupInput(BaseModel):
    key: str

registry = ToolRegistry()

async def lookup(arguments: LookupInput, context: ToolContext):
    return {"key": arguments.key, "requested_by": context.subject}

registry.register(
    "lookup_record",
    "Read one record visible to the caller.",
    LookupInput,
    lookup,
    required_scopes=("records:read",),
)

server = MCPServer("example")
registry.register_mcp_tools(
    server,
    context_provider=lambda _mcp_context: ToolContext(
        subject="service-account",
        scopes=frozenset({"records:read"}),
    ),
)
```

The registry validates every call with the declared Pydantic model, checks the caller scopes before invoking a handler, supports sync and async handlers, and returns schemas suitable for `tools/list`. MCP authentication is supplied by the host application through `context_provider`; the SDK never invents an identity or stores credentials. Error messages omit submitted values and handler exception details.
