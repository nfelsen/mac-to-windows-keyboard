import json
import os
import shlex
import stat
import tempfile
import unittest

import kvm_mcp_setup as ms

UV = "/Users/me/.local/bin/uv"
REPO = "/Users/me/My Projects/mac-to-windows-keyboard"
ARGS = ["run", "--directory", REPO, "kvm_mcp.py"]


class HarnessTests(unittest.TestCase):
    def test_all_common_assistants_listed(self):
        ids = [harness.id for harness in ms.HARNESSES]
        self.assertEqual(len(ids), len(set(ids)))
        for wanted in ("copilot-app", "copilot-cli", "vscode", "claude-desktop", "claude-code",
                       "cursor", "windsurf", "codex", "gemini", "zed"):
            self.assertIn(wanted, ids)

    def test_every_harness_has_text_and_https_source(self):
        for harness in ms.HARNESSES:
            snippet = ms.setup(harness.id, UV, REPO)
            self.assertTrue(snippet.where and snippet.steps and snippet.code, harness.id)
            self.assertTrue(harness.source.startswith("https://"), harness.id)

    def test_json_configs_parse_with_real_paths(self):
        keys = {"copilot-app": "mcpServers", "vscode": "servers", "claude-desktop": "mcpServers",
                "cursor": "mcpServers", "windsurf": "mcpServers", "gemini": "mcpServers",
                "zed": "context_servers"}
        for harness_id, key in keys.items():
            snippet = ms.setup(harness_id, UV, REPO)
            self.assertEqual(snippet.language, "json")
            server = json.loads(snippet.code)[key]["windows-keyboard"]
            self.assertEqual(server["command"], UV, harness_id)
            self.assertEqual(server["args"], ARGS, harness_id)

    def test_approval_is_never_bypassed(self):
        for harness in ms.HARNESSES:
            code = ms.setup(harness.id, UV, REPO).code.lower()
            for risky in ("autoapprove", "alwaysallow", '"trust": true', "allow-all", "never"):
                self.assertNotIn(risky, code, harness.id)
        self.assertIs(json.loads(ms.setup("gemini", UV, REPO).code)["mcpServers"]["windows-keyboard"]["trust"], False)

    def test_copilot_exposes_tools(self):
        server = json.loads(ms.setup("copilot-app", UV, REPO).code)["mcpServers"]["windows-keyboard"]
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["tools"], ["*"])

    def test_shell_commands_quote_paths(self):
        for harness_id, prefix in (("claude-code", ["claude", "mcp", "add", "--scope", "user"]),
                                   ("copilot-cli", ["copilot", "mcp", "add"])):
            words = shlex.split(ms.setup(harness_id, UV, REPO).code)
            self.assertEqual(words, prefix + ["windows-keyboard", "--", UV] + ARGS)

    def test_codex_toml(self):
        code = ms.setup("codex", UV, REPO).code
        try:
            import tomllib
        except ImportError:
            self.assertIn("[mcp_servers.windows-keyboard]", code)
            self.assertIn(json.dumps(REPO), code)
        else:
            server = tomllib.loads(code)["mcp_servers"]["windows-keyboard"]
            self.assertEqual(server, {"command": UV, "args": ARGS})
        steps = ms.setup("codex", UV, REPO).steps
        self.assertIn(shlex.quote(REPO), steps)
        self.assertNotIn("<repo>", steps)


class FindUvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def exe(self, *parts):
        path = os.path.join(self.tmp.name, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
        os.chmod(path, stat.S_IRWXU)
        return path

    def test_env_var_wins(self):
        uv = self.exe("bin", "uv")
        self.assertEqual(ms.find_uv({"UV": uv}, which=lambda _: None, home=self.tmp.name), uv)

    def test_path_then_home_fallback(self):
        on_path = self.exe("path", "uv")
        self.assertEqual(ms.find_uv({}, which=lambda _: on_path, home=self.tmp.name), on_path)
        local = self.exe(".local", "bin", "uv")
        self.assertEqual(ms.find_uv({"UV": "/missing/uv"}, which=lambda _: None, home=self.tmp.name), local)

    def test_missing_falls_back_to_bare_name(self):
        result = ms.find_uv({}, which=lambda _: None, home=self.tmp.name)
        self.assertTrue(result == "uv" or os.path.isabs(result))


class DocsTests(unittest.TestCase):
    def test_docs_in_sync_with_generator(self):
        path = os.path.join(ms.REPO, "docs", "mcp-setup.md")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), ms.markdown(),
                             "run: python3 kvm_mcp_setup.py --markdown > docs/mcp-setup.md")

    def test_docs_use_placeholders_and_cover_every_harness(self):
        text = ms.markdown()
        self.assertNotIn(os.path.expanduser("~"), text)
        for harness in ms.HARNESSES:
            self.assertIn(f'<a id="{harness.id}"></a>', text)
        self.assertTrue(ms.DOCS_URL.endswith("docs/mcp-setup.md"))


if __name__ == "__main__":
    unittest.main()
