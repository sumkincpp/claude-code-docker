#!/usr/bin/env python3
"""local-claude: Wrapper for Claude Code with Ollama support"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

# Defaults
DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5-coder:7b"
CONFIG_FILE = ".local-claude.env"


def load_config() -> Dict[str, str]:
    """Load config from .local-claude.env"""
    for path in [
        Path.home() / CONFIG_FILE,
        Path.cwd() / CONFIG_FILE,
        Path("/app") / CONFIG_FILE,
    ]:
        if path.exists():
            print(f"Loaded config: {path}")
            result: Dict[str, str] = {}
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)

                key = key.strip()
                val = val.strip().strip("\"'")

                result[key] = val
            return result
    print(f"Warning: No {CONFIG_FILE} found, using defaults\n", file=sys.stderr)
    return {}


def api_request(base_url: str, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Make Ollama API request"""
    try:
        url = base_url.rstrip("/").replace("/v1", "") + endpoint
        req = urllib.request.Request(url, method="POST" if data else "GET")
        if data:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(data).encode("utf-8")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"API error {endpoint}: {e}", file=sys.stderr)
        return None


def get_models(base_url: str) -> List[Dict[str, Any]]:
    """Get available models"""
    result = api_request(base_url, "/api/tags") or {}
    return result.get("models", [])


def supports_tools(base_url: str, model: str) -> Optional[bool]:
    """Check if model supports tools"""
    info = api_request(base_url, "/api/show", {"model": model}) or {}
    return "tools" in info.get("capabilities", [])


def list_models(config: Dict[str, str]) -> int:
    """List models with tool support info"""
    base_url = config.get("OLLAMA_BASE_URL", DEFAULT_BASE_URL)

    print(f"Connecting to {base_url}...")
    models = get_models(base_url)
    if not models:
        print("No models found or connection failed", file=sys.stderr)
        return 1

    print(f"Checking {len(models)} models...")
    print()
    categorized: Dict[str, List[tuple[str, str, float]]] = {
        "supported": [],
        "unsupported": [],
        "unknown": [],
    }

    for m in models:
        name = m.get("name", "unknown")
        size_gb = m.get("size", 0) / (1024**3)
        param = m.get("details", {}).get("parameter_size", "unknown")
        tools = supports_tools(base_url, name)

        category = "supported" if tools is True else "unsupported" if tools is False else "unknown"
        categorized[category].append((name, param, size_gb))

    if categorized["supported"]:
        print("[SUPPORTED] Tool support (recommended):")
        for name, param, size in categorized["supported"]:
            print(f"  {name:<40} ({param}, {size:.1f} GB)")

    if categorized["unknown"]:
        print()
        print("[UNKNOWN] Tool support unknown:")
        for name, param, size in categorized["unknown"]:
            print(f"  {name:<40} ({param}, {size:.1f} GB)")

    if categorized["unsupported"]:
        print()
        print("[UNSUPPORTED] No tool support:")
        for name, param, size in categorized["unsupported"]:
            print(f"  {name:<40} ({param}, {size:.1f} GB)")

    print()
    print("Recommended: Models with tool support and 32K+ context")
    return 0


def run_claude(config: Dict[str, str], model: Optional[str], extra_args: List[str]) -> int:
    """Run Claude Code with Ollama"""
    base_url = config.get("OLLAMA_BASE_URL", DEFAULT_BASE_URL)
    model = model or config.get("OLLAMA_DEFAULT_MODEL", DEFAULT_MODEL)

    print(f"Checking connection to {base_url}...")
    if not get_models(base_url):
        print("Cannot connect to Ollama", file=sys.stderr)
        return 1

    print(f"Checking model '{model}'...")
    available = [m.get("name", "") for m in get_models(base_url)]
    if not any(model == n or model in n or n in model for n in available):
        print(f"Warning: Model '{model}' not found", file=sys.stderr)
        if input("Continue? [y/N]: ").lower() != "y":
            return 1

    print("Verifying tool support...")
    tools = supports_tools(base_url, model)
    if tools is False:
        print(f"Error: Model '{model}' lacks tool support (required!)", file=sys.stderr)
        return 1
    elif tools is None:
        if input("Warning: Cannot verify tool support. Continue? [y/N]: ").lower() != "y":
            return 1
    else:
        print("Tool support confirmed")

    env = os.environ.copy()
    env.update(
        {
            "ANTHROPIC_AUTH_TOKEN": "ollama",
            "ANTHROPIC_BASE_URL": base_url,
        }
    )

    print()
    print("Starting Claude Code")
    print(f"Model: {model}")
    print(f"URL: {base_url}")
    print()

    try:
        return subprocess.run(["claude", "--model", model] + (extra_args or []), env=env).returncode
    except KeyboardInterrupt:
        print()
        print("Interrupted")
        return 130
    except FileNotFoundError:
        print("Error: 'claude' not found", file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(
        description="Claude Code wrapper for Ollama",
        epilog="Examples:\n  local-claude list\n  local-claude run\n  local-claude run -m qwen2.5-coder:7b\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="List models")
    run = sub.add_parser("run", help="Run Claude Code")
    run.add_argument("-m", "--model", help="Model override")
    run.add_argument("claude_args", nargs="*", help="Claude args")

    args = parser.parse_args()
    config = load_config()

    cmd = args.command or "run"
    if cmd == "list":
        return list_models(config)
    elif cmd == "run":
        return run_claude(config, args.model if hasattr(args, "model") else None, args.claude_args if hasattr(args, "claude_args") else [])
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
