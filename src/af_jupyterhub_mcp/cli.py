"""Command-line interface for af-jupyterhub-mcp."""

from __future__ import annotations

import argparse
import os

from af_jupyterhub_mcp.server import serve


def main() -> None:
    """Entry point for the af-jupyterhub-mcp command."""
    parser = argparse.ArgumentParser(
        prog="af-jupyterhub-mcp",
        description=(
            "MCP Server for launching and driving per-user JupyterHub servers"
            " on the ATLAS Analysis Facility"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    serve_parser = subparsers.add_parser(
        "serve",
        help="Start the MCP server (HTTP transport only)",
        description=(
            "Start the MCP server. Hub, image, site and timeout settings come"
            " from JUPYTERHUB_MCP_* environment variables."
        ),
    )
    serve_parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address (default: 0.0.0.0)",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to serve on (default: 8000)",
    )
    serve_parser.add_argument(
        "--resource-url",
        default=os.environ.get("JUPYTERHUB_MCP_RESOURCE_URL"),
        help=(
            "Externally visible base URL of this server "
            "(env: JUPYTERHUB_MCP_RESOURCE_URL; default: http://HOST:PORT)"
        ),
    )
    serve_parser.add_argument(
        "--forwarded-allow-ips",
        default="127.0.0.1",
        help="IPs trusted for X-Forwarded-* headers (default: 127.0.0.1)",
    )
    serve_parser.add_argument(
        "--log-level",
        default="info",
        help="uvicorn log level (default: info)",
    )

    args = parser.parse_args()

    if args.command == "serve":
        serve(
            host=args.host,
            port=args.port,
            resource_url=args.resource_url,
            forwarded_allow_ips=args.forwarded_allow_ips,
            log_level=args.log_level,
        )
    else:
        parser.print_help()
