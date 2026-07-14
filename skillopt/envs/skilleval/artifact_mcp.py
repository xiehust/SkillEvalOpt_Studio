"""Networkless stdio MCP facade for trusted artifact inspection."""
from __future__ import annotations

import argparse
import io
import json
import os
import stat
from typing import Annotated, Callable, cast

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import CallToolResult, ImageContent, TextContent, Tool as MCPTool
from PIL import Image as PillowImage
from PIL import UnidentifiedImageError
from pydantic import Field, WithJsonSchema

from .inspectors import (
    extract_artifact,
    inspect_artifact,
    inventory_artifacts,
    render_artifact,
)
from .inspectors.base import (
    DEFAULT_EXTRACT_CHARS,
    DEFAULT_RESPONSE_BYTES,
    DEFAULT_SCRATCH_BYTES,
    InspectionError,
    MAX_EXTRACT_CHARS,
    MAX_LOGICAL_COMPONENTS,
    MAX_LOGICAL_COMPONENT_BYTES,
    MAX_LOGICAL_PATH_BYTES,
    MAX_LOGICAL_PATH_CHARS,
    MAX_RENDER_PIXELS,
    MAX_RESPONSE_BYTES,
    MAX_SCRATCH_BYTES,
    MIN_RESPONSE_BYTES,
    ResponseBudget,
    bounded_diagnostic,
    normalize_selectors,
    validate_json_result,
    validate_logical_path,
    validate_roots,
)
from .inspectors._secure_files import open_scratch_file

DEFAULT_MAX_MEDIA_BYTES = 25 * 1024 * 1024
MAX_MEDIA_BYTES = 100 * 1024 * 1024
_PATH_ARGUMENT_DESCRIPTION = (
    "Normalized POSIX-relative evidence path; at most "
    f"{MAX_LOGICAL_PATH_CHARS} characters, {MAX_LOGICAL_PATH_BYTES} UTF-8 "
    f"bytes, {MAX_LOGICAL_COMPONENTS} components, and "
    f"{MAX_LOGICAL_COMPONENT_BYTES} UTF-8 bytes per component."
)
LogicalPath = Annotated[
    object,
    WithJsonSchema({"type": "string"}),
    Field(description=_PATH_ARGUMENT_DESCRIPTION),
]
SelectorList = Annotated[
    object,
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "array", "items": {"type": "string"}},
                {"type": "null"},
            ]
        }
    ),
]
IntegerBudget = Annotated[
    object,
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "integer"},
                {"type": "null"},
            ]
        }
    ),
]
ScratchBudget = Annotated[
    object,
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "integer"},
                {"type": "null"},
            ],
            "description": (
                "Maximum total regular-file bytes allowed under scratch."
            ),
        }
    ),
]
_PATH_REQUIRED_TOOLS = frozenset(
    {"artifact_inspect", "artifact_render", "artifact_extract"}
)
_TOOL_ARGUMENTS = {
    "artifact_inventory": frozenset(
        {"max_response_bytes", "max_scratch_bytes"}
    ),
    "artifact_inspect": frozenset(
        {"path", "max_response_bytes", "max_scratch_bytes"}
    ),
    "artifact_render": frozenset(
        {
            "path",
            "selectors",
            "max_pixels",
            "max_response_bytes",
            "max_scratch_bytes",
        }
    ),
    "artifact_extract": frozenset(
        {
            "path",
            "selectors",
            "max_extract_chars",
            "max_response_bytes",
            "max_scratch_bytes",
        }
    ),
}


class _ArtifactMCP(FastMCP):
    def __init__(self, *args, response_maximum: int, **kwargs):
        self._response_maximum = response_maximum
        super().__init__(*args, **kwargs)

    async def list_tools(self) -> list[MCPTool]:
        """Advertise required paths while runtime validation stays guarded."""
        tools = await super().list_tools()
        advertised: list[MCPTool] = []
        for tool in tools:
            if tool.name not in _PATH_REQUIRED_TOOLS:
                advertised.append(tool)
                continue
            schema = dict(tool.inputSchema)
            required = list(schema.get("required", []))
            if "path" not in required:
                required.append("path")
            schema["required"] = required
            advertised.append(
                tool.model_copy(update={"inputSchema": schema})
            )
        return advertised

    async def call_tool(self, name: str, arguments: dict):
        allowed = _TOOL_ARGUMENTS.get(name)
        if allowed is not None:
            unknown = sorted(set(arguments) - allowed)
            if unknown:
                def reject(_response_limit: int) -> CallToolResult:
                    raise InspectionError("unknown tool argument")

                return _guarded_tool(
                    name,
                    arguments.get("max_response_bytes"),
                    self._response_maximum,
                    reject,
                )
        return await super().call_tool(name, arguments)


def _positive_maximum(value: object, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InspectionError(f"{name} must be a positive integer")
    if value > maximum:
        raise InspectionError(f"{name} exceeds maximum {maximum}")
    return value


def _request_budget(
    value: object,
    *,
    default: int,
    maximum: int,
    name: str,
) -> int:
    selected = default if value is None else value
    return _positive_maximum(selected, name, maximum)


def _select_response_budget(
    requested: object,
    server_maximum: int,
) -> int:
    if requested is None:
        return server_maximum
    if (
        isinstance(requested, bool)
        or not isinstance(requested, int)
        or requested < MIN_RESPONSE_BYTES
    ):
        raise InspectionError(
            f"response byte budget must be at least {MIN_RESPONSE_BYTES}"
        )
    return min(requested, server_maximum)


def _envelope(operation: str, *, result=None, error: str | None = None) -> dict:
    body = {
        "trust": "untrusted_evidence",
        "operation": operation,
        "status": "error" if error is not None else "ok",
    }
    if error is None:
        body["result"] = result
    else:
        body["error"] = error
    return {"untrusted_evidence": body}


def _tool_result(
    payload: dict,
    *,
    response_limit: int,
    images: list[ImageContent] | None = None,
    is_error: bool = False,
) -> CallToolResult:
    budget = ResponseBudget(
        max_bytes=response_limit,
        max_extract_chars=MAX_EXTRACT_CHARS,
    )
    validate_json_result(payload, budget)
    text = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    content = [TextContent(type="text", text=text)]
    content.extend(images or [])
    result = CallToolResult(
        content=content,
        structuredContent=payload,
        isError=is_error,
    )
    serialized = result.model_dump_json(
        by_alias=True,
        exclude_none=True,
    ).encode("utf-8")
    if len(serialized) > response_limit:
        raise InspectionError(
            "MCP response byte budget exceeded: "
            f"{len(serialized)} > {response_limit}"
        )
    return result


def _error_result(
    operation: str,
    exc: Exception,
    *,
    response_limit: int,
) -> CallToolResult:
    diagnostic_limit = max(80, min(512, response_limit // 2))
    error = (
        f"{type(exc).__name__}: "
        f"{bounded_diagnostic(exc, diagnostic_limit)}"
    )
    payload = _envelope(operation, error=error)
    try:
        return _tool_result(
            payload,
            response_limit=response_limit,
            is_error=True,
        )
    except Exception:
        fallback = _envelope(
            operation,
            error="InspectionError: bounded artifact operation failure",
        )
        return _tool_result(
            fallback,
            response_limit=response_limit,
            is_error=True,
        )


def _guarded_tool(
    operation: str,
    requested_response_limit: object,
    server_response_maximum: int,
    function: Callable[[int], CallToolResult],
) -> CallToolResult:
    try:
        response_limit = _select_response_budget(
            requested_response_limit,
            server_response_maximum,
        )
    except Exception as exc:
        return _error_result(
            operation,
            exc,
            response_limit=server_response_maximum,
        )
    try:
        return function(response_limit)
    except Exception as exc:
        return _error_result(
            operation,
            exc,
            response_limit=response_limit,
        )


def _read_png(
    path: str,
    scratch_dir: str,
    *,
    remaining_pixels: int,
    remaining_bytes: int,
) -> tuple[bytes, dict]:
    try:
        descriptor = open_scratch_file(scratch_dir, path)
    except (OSError, InspectionError) as exc:
        raise InspectionError(
            f"rendered media could not be opened: {bounded_diagnostic(exc)}"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise InspectionError("rendered media must be a regular file")
        if info.st_size <= 0 or info.st_size > remaining_bytes:
            raise InspectionError("rendered media byte budget exceeded")
        with os.fdopen(os.dup(descriptor), "rb") as source:
            data = source.read(remaining_bytes + 1)
    finally:
        os.close(descriptor)
    if len(data) != info.st_size or len(data) > remaining_bytes:
        raise InspectionError("rendered media byte budget exceeded")
    try:
        with PillowImage.open(io.BytesIO(data)) as image:
            if image.format != "PNG":
                raise InspectionError("rendered media must be PNG")
            width, height = image.size
            image.verify()
    except InspectionError:
        raise
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise InspectionError(
            f"rendered media is not a valid PNG: {bounded_diagnostic(exc)}"
        ) from exc
    pixels = width * height
    if width <= 0 or height <= 0 or pixels > remaining_pixels:
        raise InspectionError("rendered media pixel budget exceeded")
    return data, {
        "mime": "image/png",
        "width": width,
        "height": height,
        "bytes": len(data),
        "pixels": pixels,
    }


def create_server(
    evidence_dir: str,
    scratch_dir: str,
    *,
    max_render_pixels: int = MAX_RENDER_PIXELS,
    max_response_bytes: int = DEFAULT_RESPONSE_BYTES,
    max_extract_chars: int = DEFAULT_EXTRACT_CHARS,
    max_media_bytes: int = DEFAULT_MAX_MEDIA_BYTES,
    max_scratch_bytes: int = DEFAULT_SCRATCH_BYTES,
) -> FastMCP:
    """Create an in-memory or stdio Artifact MCP server."""
    evidence, scratch = validate_roots(evidence_dir, scratch_dir)
    server_render_max = _positive_maximum(
        max_render_pixels,
        "server render pixel maximum",
        MAX_RENDER_PIXELS,
    )
    server_response_max = _positive_maximum(
        max_response_bytes,
        "server response byte maximum",
        MAX_RESPONSE_BYTES,
    )
    if server_response_max < MIN_RESPONSE_BYTES:
        raise InspectionError(
            f"server response byte maximum must be at least "
            f"{MIN_RESPONSE_BYTES}"
        )
    server_extract_max = _positive_maximum(
        max_extract_chars,
        "server extraction character maximum",
        MAX_EXTRACT_CHARS,
    )
    server_media_max = _positive_maximum(
        max_media_bytes,
        "server media byte maximum",
        MAX_MEDIA_BYTES,
    )
    server_scratch_max = _positive_maximum(
        max_scratch_bytes,
        "server scratch byte maximum",
        MAX_SCRATCH_BYTES,
    )

    server = _ArtifactMCP(
        "skillopt-artifact",
        instructions=None,
        response_maximum=server_response_max,
    )

    @server.tool(
        name="artifact_inventory",
        description="List immutable evidence artifacts with bounded metadata.",
    )
    def artifact_inventory(
        max_response_bytes: IntegerBudget = None,
        max_scratch_bytes: ScratchBudget = None,
    ) -> CallToolResult:
        def run(response_limit: int) -> CallToolResult:
            scratch_limit = _request_budget(
                max_scratch_bytes,
                default=server_scratch_max,
                maximum=server_scratch_max,
                name="scratch byte budget",
            )
            result = inventory_artifacts(
                evidence,
                scratch,
                max_response_bytes=response_limit,
                max_scratch_bytes=scratch_limit,
            )
            return _tool_result(
                _envelope("artifact_inventory", result=result),
                response_limit=response_limit,
            )

        return _guarded_tool(
            "artifact_inventory",
            max_response_bytes,
            server_response_max,
            run,
        )

    @server.tool(
        name="artifact_inspect",
        description=(
            "Inspect one logical evidence artifact. "
            + _PATH_ARGUMENT_DESCRIPTION
        ),
    )
    def artifact_inspect(
        path: LogicalPath = None,
        max_response_bytes: IntegerBudget = None,
        max_scratch_bytes: ScratchBudget = None,
    ) -> CallToolResult:
        def run(response_limit: int) -> CallToolResult:
            validate_logical_path(path)
            scratch_limit = _request_budget(
                max_scratch_bytes,
                default=server_scratch_max,
                maximum=server_scratch_max,
                name="scratch byte budget",
            )
            result = inspect_artifact(
                cast(str, path),
                evidence_dir=evidence,
                scratch_dir=scratch,
                max_response_bytes=response_limit,
                max_scratch_bytes=scratch_limit,
            )
            return _tool_result(
                _envelope("artifact_inspect", result=result),
                response_limit=response_limit,
            )

        return _guarded_tool(
            "artifact_inspect",
            max_response_bytes,
            server_response_max,
            run,
        )

    @server.tool(
        name="artifact_render",
        description=(
            "Render selected artifact units as bounded PNG content. "
            + _PATH_ARGUMENT_DESCRIPTION
        ),
    )
    def artifact_render(
        path: LogicalPath = None,
        selectors: SelectorList = None,
        max_pixels: IntegerBudget = None,
        max_response_bytes: IntegerBudget = None,
        max_scratch_bytes: ScratchBudget = None,
    ) -> CallToolResult:
        def run(response_limit: int) -> CallToolResult:
            validate_logical_path(path)
            normalized_selectors = normalize_selectors(selectors)
            pixel_limit = _request_budget(
                max_pixels,
                default=server_render_max,
                maximum=server_render_max,
                name="render pixel budget",
            )
            scratch_limit = _request_budget(
                max_scratch_bytes,
                default=server_scratch_max,
                maximum=server_scratch_max,
                name="scratch byte budget",
            )
            paths = render_artifact(
                cast(str, path),
                evidence_dir=evidence,
                scratch_dir=scratch,
                selectors=normalized_selectors,
                max_pixels=pixel_limit,
                max_response_bytes=response_limit,
                max_scratch_bytes=scratch_limit,
            )
            metadata = []
            images = []
            used_pixels = 0
            used_bytes = 0
            for index, rendered_path in enumerate(paths):
                data, details = _read_png(
                    rendered_path,
                    scratch,
                    remaining_pixels=pixel_limit - used_pixels,
                    remaining_bytes=server_media_max - used_bytes,
                )
                used_pixels += details.pop("pixels")
                used_bytes += details["bytes"]
                metadata.append({"index": index, **details})
                images.append(Image(data=data, format="png").to_image_content())
            result = {"images": metadata}
            return _tool_result(
                _envelope("artifact_render", result=result),
                response_limit=response_limit,
                images=images,
            )

        return _guarded_tool(
            "artifact_render",
            max_response_bytes,
            server_response_max,
            run,
        )

    @server.tool(
        name="artifact_extract",
        description=(
            "Extract bounded content from selected artifact units. "
            + _PATH_ARGUMENT_DESCRIPTION
        ),
    )
    def artifact_extract(
        path: LogicalPath = None,
        selectors: SelectorList = None,
        max_extract_chars: IntegerBudget = None,
        max_response_bytes: IntegerBudget = None,
        max_scratch_bytes: ScratchBudget = None,
    ) -> CallToolResult:
        def run(response_limit: int) -> CallToolResult:
            validate_logical_path(path)
            normalized_selectors = normalize_selectors(selectors)
            extract_limit = _request_budget(
                max_extract_chars,
                default=server_extract_max,
                maximum=server_extract_max,
                name="extraction character budget",
            )
            scratch_limit = _request_budget(
                max_scratch_bytes,
                default=server_scratch_max,
                maximum=server_scratch_max,
                name="scratch byte budget",
            )
            result = extract_artifact(
                cast(str, path),
                evidence_dir=evidence,
                scratch_dir=scratch,
                selectors=normalized_selectors,
                max_extract_chars=extract_limit,
                max_response_bytes=response_limit,
                max_scratch_bytes=scratch_limit,
            )
            return _tool_result(
                _envelope("artifact_extract", result=result),
                response_limit=response_limit,
            )

        return _guarded_tool(
            "artifact_extract",
            max_response_bytes,
            server_response_max,
            run,
        )

    return server


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise InspectionError(f"{name} must be an integer") from exc


def main(argv: list[str] | None = None) -> None:
    """Run only the stdio transport inside the caller-provided sandbox."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence",
        default=os.environ.get("SKILLOPT_ARTIFACT_EVIDENCE", "/evidence"),
    )
    parser.add_argument(
        "--scratch",
        default=os.environ.get("SKILLOPT_ARTIFACT_SCRATCH", "/scratch"),
    )
    parser.add_argument(
        "--max-render-pixels",
        type=int,
        default=_env_int(
            "SKILLOPT_ARTIFACT_MAX_RENDER_PIXELS",
            MAX_RENDER_PIXELS,
        ),
    )
    parser.add_argument(
        "--max-response-bytes",
        type=int,
        default=_env_int(
            "SKILLOPT_ARTIFACT_MAX_RESPONSE_BYTES",
            DEFAULT_RESPONSE_BYTES,
        ),
    )
    parser.add_argument(
        "--max-extract-chars",
        type=int,
        default=_env_int(
            "SKILLOPT_ARTIFACT_MAX_EXTRACT_CHARS",
            DEFAULT_EXTRACT_CHARS,
        ),
    )
    parser.add_argument(
        "--max-media-bytes",
        type=int,
        default=_env_int(
            "SKILLOPT_ARTIFACT_MAX_MEDIA_BYTES",
            DEFAULT_MAX_MEDIA_BYTES,
        ),
    )
    parser.add_argument(
        "--max-scratch-bytes",
        type=int,
        default=_env_int(
            "SKILLOPT_ARTIFACT_MAX_SCRATCH_BYTES",
            DEFAULT_SCRATCH_BYTES,
        ),
    )
    args = parser.parse_args(argv)
    server = create_server(
        args.evidence,
        args.scratch,
        max_render_pixels=args.max_render_pixels,
        max_response_bytes=args.max_response_bytes,
        max_extract_chars=args.max_extract_chars,
        max_media_bytes=args.max_media_bytes,
        max_scratch_bytes=args.max_scratch_bytes,
    )
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
