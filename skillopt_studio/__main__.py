"""CLI entry point: ``python3 -m skillopt_studio [--host] [--port] [--reload]``."""
from __future__ import annotations

import argparse

import uvicorn


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="skillopt_studio",
        description="SkillOpt Studio — localhost web console for skill evaluation and training",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8321, help="port (default: 8321)")
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes (dev)")
    args = parser.parse_args(argv)

    uvicorn.run(
        "skillopt_studio.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
