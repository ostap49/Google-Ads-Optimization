"""Tool registry: modules declare tools with @tool, server.py registers them with MCP.

Wrapping keeps tool functions plain/testable and turns API/validation errors into compact
JSON error payloads instead of stack traces.
"""
from __future__ import annotations

import functools
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable

from gads.client import GadsError

log = logging.getLogger("adsops.tools")


@dataclass
class ToolSpec:
    fn: Callable[..., Any]
    name: str
    read_only: bool
    destructive: bool
    title: str | None


TOOLS: dict[str, ToolSpec] = {}


def tool(read_only: bool = True, destructive: bool = False, name: str | None = None, title: str | None = None):
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await fn(*args, **kwargs)
                except GadsError as ex:
                    return ex.as_dict()
                except (ValueError, KeyError, PermissionError) as ex:
                    return {"error": str(ex)}
        else:
            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    return fn(*args, **kwargs)
                except GadsError as ex:
                    return ex.as_dict()
                except (ValueError, KeyError, PermissionError) as ex:
                    return {"error": str(ex)}
        spec = ToolSpec(wrapper, name or fn.__name__, read_only, destructive, title)
        TOOLS[spec.name] = spec
        wrapper.raw = fn  # type: ignore[attr-defined]
        return wrapper
    return deco
