"""Versioned, typed tool registration for agent applications."""

from importlib.metadata import PackageNotFoundError, version

from a2a_tools_sdk.registry import (
    ToolContext,
    ToolInvocationError,
    ToolRegistry,
    ToolSpec,
)

try:
    __version__ = version("a2a-tools-sdk")
except PackageNotFoundError:  # pragma: no cover - source tree before installation
    __version__ = "0.1.0"

__all__ = ["ToolContext", "ToolInvocationError", "ToolRegistry", "ToolSpec", "__version__"]
