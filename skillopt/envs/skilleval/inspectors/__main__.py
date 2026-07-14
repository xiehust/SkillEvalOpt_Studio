"""Trusted JSON command-line interface for artifact inspection."""
from __future__ import annotations

import argparse
import json
import sys

from . import (
    extract_artifact,
    inspect_artifact,
    inventory_artifacts,
    render_artifact,
)
from .base import (
    DEFAULT_EXTRACT_CHARS,
    DEFAULT_RESPONSE_BYTES,
    InspectionError,
    MAX_EXTRACT_CHARS,
    MAX_RENDER_PIXELS,
    MAX_RESPONSE_BYTES,
    bounded_diagnostic,
    normalize_selectors,
    validate_roots,
)


class _JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise InspectionError(f"argument error: {message}")


def _bounded_int(name: str, maximum: int):
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{name} must be an integer") from exc
        if parsed <= 0:
            raise argparse.ArgumentTypeError(f"{name} must be positive")
        if parsed > maximum:
            raise argparse.ArgumentTypeError(
                f"{name} exceeds maximum {maximum}"
            )
        return parsed

    return parse


def _add_roots(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--scratch", required=True)
    parser.add_argument(
        "--max-response-bytes",
        type=_bounded_int("max response bytes", MAX_RESPONSE_BYTES),
        default=DEFAULT_RESPONSE_BYTES,
    )


def _add_artifact(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("artifact")
    parser.add_argument("--selector", action="append", default=[])
    _add_roots(parser)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = _JSONArgumentParser(
        prog="skillopt-artifactctl",
        description="Inspect immutable evidence artifacts.",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_JSONArgumentParser,
    )

    inventory = subparsers.add_parser("inventory")
    _add_roots(inventory)

    inspect_parser = subparsers.add_parser("inspect")
    _add_artifact(inspect_parser)

    render = subparsers.add_parser("render")
    _add_artifact(render)
    render.add_argument(
        "--max-pixels",
        type=_bounded_int("max pixels", MAX_RENDER_PIXELS),
        default=MAX_RENDER_PIXELS,
    )

    extract = subparsers.add_parser("extract")
    _add_artifact(extract)
    extract.add_argument(
        "--max-extract-chars",
        type=_bounded_int("max extract chars", MAX_EXTRACT_CHARS),
        default=DEFAULT_EXTRACT_CHARS,
    )
    return parser.parse_args(argv)


def _serialize(payload: dict, maximum: int) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > maximum:
        raise InspectionError("CLI response byte budget exceeded")
    return encoded


def main(argv: list[str] | None = None) -> int:
    """Run one operation and emit exactly one JSON object on stdout."""
    response_limit = DEFAULT_RESPONSE_BYTES
    try:
        args = parse_args(argv)
        response_limit = args.max_response_bytes
        validate_roots(args.evidence, args.scratch)
        if args.command == "inventory":
            result = inventory_artifacts(
                args.evidence,
                args.scratch,
                max_response_bytes=args.max_response_bytes,
            )
        elif args.command == "inspect":
            normalize_selectors(args.selector)
            result = inspect_artifact(
                args.artifact,
                evidence_dir=args.evidence,
                scratch_dir=args.scratch,
                max_response_bytes=args.max_response_bytes,
            )
        elif args.command == "render":
            result = render_artifact(
                args.artifact,
                evidence_dir=args.evidence,
                scratch_dir=args.scratch,
                selectors=args.selector,
                max_pixels=args.max_pixels,
                max_response_bytes=args.max_response_bytes,
            )
        else:
            result = extract_artifact(
                args.artifact,
                evidence_dir=args.evidence,
                scratch_dir=args.scratch,
                selectors=args.selector,
                max_extract_chars=args.max_extract_chars,
                max_response_bytes=args.max_response_bytes,
            )
        output = _serialize(
            {"status": "ok", "result": result},
            response_limit,
        )
        exit_code = 0
    except Exception as exc:
        error = f"{type(exc).__name__}: {bounded_diagnostic(exc)}"
        payload = {"status": "error", "error": error}
        try:
            output = _serialize(payload, max(response_limit, 2_000))
        except Exception:
            output = json.dumps(
                {
                    "status": "error",
                    "error": "InspectionError: bounded error response failed",
                },
                separators=(",", ":"),
            )
        exit_code = 2
    sys.stdout.write(output + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
