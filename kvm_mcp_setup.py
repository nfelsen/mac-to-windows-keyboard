#!/usr/bin/env python3
"""MCP setup snippets for common AI assistants.

Generates copy-pasteable configs that register kvm_mcp.py (server name
"windows-keyboard") with the real paths to uv and this checkout. Used by the
"Connect AI Assistant" window in kvm_app.py and to generate docs/mcp-setup.md.

    python3 kvm_mcp_setup.py                     # all assistants, real paths
    python3 kvm_mcp_setup.py --harness cursor    # one assistant
    python3 kvm_mcp_setup.py --markdown > docs/mcp-setup.md
"""

import argparse
import json
import os
import shlex
import shutil
import sys
from typing import Callable, List, NamedTuple, Optional

SERVER = "windows-keyboard"
REPO = os.path.dirname(os.path.abspath(__file__))
DOCS_URL = "https://github.com/nfelsen/mac-to-windows-keyboard/blob/main/docs/mcp-setup.md"
PLACEHOLDER_UV = "/Users/YOU/.local/bin/uv"
PLACEHOLDER_REPO = "/Users/YOU/src/mac-to-windows-keyboard"

INTRO = ("Let an AI assistant type commands on Windows through the cable. "
         "Pick your assistant, copy the config, and restart the assistant. "
         "Each call asks for your approval before anything is typed.")
SAFETY = ("The assistant can't see the Windows screen; it will ask you what happened. "
          "Keep tool approval on: don't enable auto-approve, \"trust\", "
          "\"always allow\" or similar settings for this server, so you review "
          "every command before it is typed.")


class Setup(NamedTuple):
    name: str
    where: str
    steps: str
    language: str
    code: str


class Harness(NamedTuple):
    id: str
    name: str
    where: str
    steps: str
    language: str
    render: Callable[[str, List[str]], str]
    source: str


def server_args(repo: str) -> List[str]:
    return ["run", "--directory", repo, "kvm_mcp.py"]


def _json(data) -> str:
    return json.dumps(data, indent=2)


def _plain(command: str, args: List[str]) -> dict:
    return {"command": command, "args": args}


def _mcp_servers(command, args, **extra):
    return _json({"mcpServers": {SERVER: dict(_plain(command, args), **extra)}})


def _shell(words: List[str]) -> str:
    return " ".join(shlex.quote(word) for word in words)


def _toml_str(value: str) -> str:
    # JSON string escapes are valid TOML basic-string escapes.
    return json.dumps(value)


def _copilot(command, args):
    return _mcp_servers(command, args, type="stdio", tools=["*"])


def _copilot_cli(command, args):
    return _shell(["copilot", "mcp", "add", SERVER, "--", command] + args)


def _vscode(command, args):
    return _json({"servers": {SERVER: dict(type="stdio", **_plain(command, args))}})


def _claude_code(command, args):
    return _shell(["claude", "mcp", "add", "--scope", "user", SERVER, "--", command] + args)


def _codex(command, args):
    items = ", ".join(_toml_str(arg) for arg in args)
    return (f"[mcp_servers.{SERVER}]\n"
            f"command = {_toml_str(command)}\n"
            f"args = [{items}]")


def _gemini(command, args):
    return _mcp_servers(command, args, trust=False)


def _zed(command, args):
    return _json({"context_servers": {SERVER: dict(_plain(command, args), env={})}})


HARNESSES = [
    Harness("copilot-app", "GitHub Copilot app",
            "Customize → MCP → add a server, or ~/.copilot/mcp-config.json",
            "Add a local (stdio) server named windows-keyboard with the command and "
            "arguments below, or merge this JSON into ~/.copilot/mcp-config.json "
            "(shared with Copilot CLI). Restart the app.",
            "json", _copilot,
            "https://docs.github.com/en/copilot/how-tos/github-copilot-app/customize-github-copilot-app"),
    Harness("copilot-cli", "GitHub Copilot CLI",
            "Terminal, or ~/.copilot/mcp-config.json",
            "Run this in a terminal. It writes ~/.copilot/mcp-config.json (the same "
            "JSON as the GitHub Copilot app). You can also use /mcp add inside a session. "
            "Don't start Copilot with --allow-all-tools for this server.",
            "bash", _copilot_cli,
            "https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers"),
    Harness("vscode", "VS Code (Copilot Chat)",
            ".vscode/mcp.json, or Command Palette → MCP: Open User Configuration",
            "Merge this into .vscode/mcp.json for one workspace, or into the user "
            "configuration for all workspaces. Start the server from the MCP view and "
            "use it in Agent mode. Keep VS Code's trust and tool confirmation prompts.",
            "json", _vscode,
            "https://code.visualstudio.com/docs/agents/reference/mcp-configuration"),
    Harness("claude-desktop", "Claude Desktop",
            "~/Library/Application Support/Claude/claude_desktop_config.json",
            "Open Settings → Developer → Edit Config, merge this JSON into "
            "claude_desktop_config.json, then quit and reopen Claude. Leave tool "
            "permissions on \"Ask\" for this server.",
            "json", lambda c, a: _mcp_servers(c, a),
            "https://modelcontextprotocol.io/docs/develop/connect-local-servers"),
    Harness("claude-code", "Claude Code",
            "Terminal (stored in ~/.claude.json)",
            "Run this in a terminal. --scope user makes the server available in every "
            "project. Check it with claude mcp list. Don't add its tools to the "
            "allow list or use --dangerously-skip-permissions.",
            "bash", _claude_code,
            "https://code.claude.com/docs/en/mcp"),
    Harness("cursor", "Cursor",
            "~/.cursor/mcp.json (or .cursor/mcp.json in a project)",
            "Merge this JSON into ~/.cursor/mcp.json, then check that windows-keyboard "
            "is enabled in Cursor Settings → MCP. Keep the default approval mode so the "
            "agent asks before each tool call.",
            "json", lambda c, a: _mcp_servers(c, a),
            "https://cursor.com/help/customization/mcp"),
    Harness("windsurf", "Windsurf",
            "~/.codeium/windsurf/mcp_config.json",
            "In Windsurf open Cascade → MCP servers → View raw config (normally "
            "~/.codeium/windsurf/mcp_config.json), merge this JSON, and refresh the "
            "server list. Don't turn on auto-execution for this server.",
            "json", lambda c, a: _mcp_servers(c, a),
            "https://docs.windsurf.com/windsurf/cascade/mcp"),
    Harness("codex", "OpenAI Codex CLI",
            "~/.codex/config.toml",
            "Add this to ~/.codex/config.toml, or run: codex mcp add windows-keyboard "
            "-- <uv> run --directory <repo> kvm_mcp.py. Keep approval_policy at its "
            "default (\"on-request\"); don't set it to \"never\".",
            "toml", _codex,
            "https://developers.openai.com/codex/mcp"),
    Harness("gemini", "Gemini CLI",
            "~/.gemini/settings.json (or .gemini/settings.json in a project)",
            "Merge this into ~/.gemini/settings.json and restart Gemini CLI. Check it "
            "with /mcp. Keep \"trust\": false; true skips every confirmation.",
            "json", _gemini,
            "https://geminicli.com/docs/tools/mcp-server/"),
    Harness("zed", "Zed",
            "~/.config/zed/settings.json",
            "Merge this into ~/.config/zed/settings.json (or use Settings → AI → MCP "
            "Servers → Add Local Server). Keep agent.tool_permissions.default at "
            "\"confirm\".",
            "json", _zed,
            "https://zed.dev/docs/ai/mcp"),
]
_BY_ID = {harness.id: harness for harness in HARNESSES}


def _fill(text: str, uv: str, repo: str) -> str:
    return text.replace("<uv>", shlex.quote(uv)).replace("<repo>", shlex.quote(repo))


def setup(harness_id: str, uv: str, repo: str = REPO) -> Setup:
    """The snippet and instructions for one assistant."""
    harness = _BY_ID[harness_id]
    return Setup(harness.name, harness.where, _fill(harness.steps, uv, repo),
                 harness.language, harness.render(uv, server_args(repo)))


def find_uv(environ=None, which=shutil.which, home: Optional[str] = None) -> str:
    """Absolute path to uv, which GUI assistants need (they don't use your shell PATH)."""
    environ = os.environ if environ is None else environ
    candidates = [environ.get("UV"), which("uv")]
    home = home or os.path.expanduser("~")
    candidates += [os.path.join(home, ".local", "bin", "uv"),
                   os.path.join(home, ".cargo", "bin", "uv"),
                   "/opt/homebrew/bin/uv", "/usr/local/bin/uv"]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return os.path.abspath(candidate)
    return "uv"


def markdown(uv: str = PLACEHOLDER_UV, repo: str = PLACEHOLDER_REPO) -> str:
    """docs/mcp-setup.md, generated from HARNESSES."""
    out = [
        "# Connect an AI assistant (MCP setup)",
        "",
        "<!-- Generated by `python3 kvm_mcp_setup.py --markdown`; edit kvm_mcp_setup.py instead. -->",
        "",
        "`kvm_mcp.py` is a local stdio [MCP](https://modelcontextprotocol.io/) server named "
        "`windows-keyboard`. It lets an AI assistant type on the Windows PC through the cable, "
        "after you approve each call. See the README for its tools.",
        "",
        "The easiest way to get a ready-to-paste config is the app: click **Connect AI Assistant…** "
        "(or Help → Connect AI Assistant). It fills in your real paths and has a Copy button. "
        "You can also print them from a terminal:",
        "",
        "```bash",
        "python3 kvm_mcp_setup.py                   # every assistant",
        "python3 kvm_mcp_setup.py --harness cursor  # just one",
        "```",
        "",
        f"The examples below use `{uv}` for uv and `{repo}` for this repository. Replace both "
        "with absolute paths (`which uv` and `pwd` in your clone). GUI apps don't read your shell "
        "`PATH`, so use the full path to `uv`. If a config file already has other servers, add the "
        "`windows-keyboard` entry next to them.",
        "",
        "> **Safety:** " + SAFETY,
        "",
        "Assistants:",
        "",
    ]
    for harness in HARNESSES:
        out.append(f"- [{harness.name}](#{harness.id})")
    for harness in HARNESSES:
        snippet = setup(harness.id, uv, repo)
        out += [
            "",
            f'<a id="{harness.id}"></a>',
            "",
            f"## {harness.name}",
            "",
            f"**Where:** {harness.where}",
            "",
            snippet.steps,
            "",
            f"```{snippet.language}",
            snippet.code,
            "```",
            "",
            f"Reference: <{harness.source}>",
        ]
    out += [
        "",
        "## Check it",
        "",
        "Ask the assistant to run the `windows_keyboard_status` tool. It types nothing and reports "
        "the port, baud rate, whether Windows sees the keyboard, and Caps Lock. From a terminal:",
        "",
        "```bash",
        f"{_shell([uv] + server_args(repo))} --status",
        "```",
        "",
    ]
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--harness", choices=[harness.id for harness in HARNESSES])
    parser.add_argument("--markdown", action="store_true",
                        help="print docs/mcp-setup.md (placeholder paths)")
    args = parser.parse_args(argv)
    if args.markdown:
        sys.stdout.write(markdown())
        return 0
    uv = find_uv()
    for harness in HARNESSES:
        if args.harness and harness.id != args.harness:
            continue
        snippet = setup(harness.id, uv)
        print(f"== {snippet.name} ==\nWhere: {snippet.where}\n{snippet.steps}\n\n{snippet.code}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
