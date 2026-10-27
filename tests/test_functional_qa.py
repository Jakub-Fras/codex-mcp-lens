"""Regression checks for settings edits used by the cleanup commands."""

import contextlib
import datetime as dt
import io
import json
import sqlite3
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_lens


class SettingsEditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = Path(temporary.name) / "config.toml"
        self.row = {"id": "plugin:demo@local:bundled", "name": "bundled",
                    "plugin_key": "demo@local"}

    def test_cli_refuses_inline_plugin_server_without_changing_config(self):
        for original in (
            '[plugins."demo@local"]\nmcp_servers = { bundled = { enabled = true } }\n',
            '[plugins."demo@local".mcp_servers]\nbundled = { enabled = true }\n',
        ):
            with self.subTest(original=original):
                self.config.write_text(original, encoding="utf-8")
                with patch("builtins.input", return_value=self.row["id"]), \
                        contextlib.redirect_stdout(io.StringIO()), \
                        self.assertRaisesRegex(SystemExit, "cannot edit safely"):
                    mcp_lens.remove_server(self.config, self.row)
                self.assertEqual(self.config.read_text(encoding="utf-8"), original)

    def test_cli_disables_plugin_server_without_changing_nested_env(self):
        original = ('[plugins."demo@local".mcp_servers.bundled]\n'
                    'command = "python"\n'
                    '[plugins."demo@local".mcp_servers.bundled.env]\n'
                    'PRIVATE = "saved"\n')
        self.config.write_text(original, encoding="utf-8")
        with patch("builtins.input", return_value=self.row["id"]), \
                contextlib.redirect_stdout(io.StringIO()):
            mcp_lens.remove_server(self.config, self.row)
        parsed = tomllib.loads(self.config.read_text(encoding="utf-8"))
        entry = parsed["plugins"]["demo@local"]["mcp_servers"]["bundled"]
        self.assertIs(entry["enabled"], False)
        self.assertEqual(entry["env"], {"PRIVATE": "saved"})

    def test_bad_saved_starting_folder_does_not_crash_or_hide_valid_folder(self):
        home = self.config.parent
        state = sqlite3.connect(home / "state_test.sqlite")
        state.execute("CREATE TABLE threads (id TEXT, cwd TEXT)")
        state.executemany("INSERT INTO threads VALUES (?, ?)", (
            ("damaged", "/tmp/\x00bad"),
            ("usable", str(home)),
        ))
        state.commit()
        state.close()
        session = home / "sessions" / "bad.jsonl"
        session.parent.mkdir()
        session.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": "damaged-session", "cwd": "/tmp/\u0000bad"}}) + "\n", encoding="utf-8")
        coverage = {"folder_files_unsupported": 0, "folder_files_checked": 0,
                    "folder_files_unreadable": 0, "folder_conflicts": 0}
        folders = mcp_lens.thread_folders(home, coverage)
        self.assertEqual(folders, {"usable": str(home.resolve())})
        self.assertEqual(coverage["folder_files_checked"], 1)

    def test_two_cached_plugin_versions_make_one_entry_with_all_sources(self):
        home = self.config.parent
        self.config.write_text('[plugins."demo@local"]\nenabled = true\n', encoding="utf-8")
        for version in ("1.0.0", "2.0.0"):
            manifest = home / "plugins/cache/local/demo" / version / ".mcp.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"mcpServers": {
                "bundled": {"command": "python"}}}), encoding="utf-8")
        _, rows = mcp_lens.load_inventory(self.config, home)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], self.row["id"])
        self.assertNotIn("history_note", rows[0])
        self.assertEqual(len(rows[0]["manifest_paths"]), 2)
        mcp_lens.annotate_source_locations(rows, self.config)
        self.assertEqual(len([entry for entry in rows[0]["source_locations"]
                              if entry["label"] == "Saved plugin file"]), 2)

    def test_conflicting_cached_plugin_versions_hide_unverified_endpoint(self):
        home = self.config.parent
        self.config.write_text('[plugins."demo@local"]\nenabled = true\n', encoding="utf-8")
        for version, command in (("1.0.0", "first"), ("2.0.0", "second")):
            manifest = home / "plugins/cache/local/demo" / version / ".mcp.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"mcpServers": {
                "bundled": {"command": command}}}), encoding="utf-8")
        _, rows = mcp_lens.load_inventory(self.config, home)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["command"])
        self.assertIn("active version could not be identified", rows[0]["version_note"])

    def test_invalid_utf8_cache_files_do_not_crash_inventory(self):
        home = self.config.parent
        self.config.write_text("", encoding="utf-8")
        cache_files = (
            home / "plugins/cache/local/demo/1.0.0/.mcp.json",
            home / "plugins/cache/local/demo/1.0.0/.app.json",
            home / "cache/codex_app_directory/broken.json",
            home / "cache/codex_apps_tools/broken.json",
        )
        for path in cache_files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\xff\xfe")
        _, rows = mcp_lens.load_inventory(self.config, home)
        self.assertEqual(rows, [])

    def test_invalid_utf8_snapshot_is_left_untouched(self):
        snapshot = self.config.parent / "changes.json"
        snapshot.write_bytes(b"\xff\xfe")
        changes, note = mcp_lens.snapshot_inventory(snapshot, [])
        self.assertEqual(changes, [])
        self.assertIn("could not be read", note)
        self.assertEqual(snapshot.read_bytes(), b"\xff\xfe")

    def test_cli_refuses_to_disable_built_in_codex_feature(self):
        original = '[plugins."codex-app-tools@openai-bundled"]\nenabled = true\n'
        self.config.write_text(original, encoding="utf-8")
        row = {"id": "plugin:codex-app-tools@openai-bundled:codex_app",
               "name": "codex_app", "plugin_key": "codex-app-tools@openai-bundled"}
        with patch("builtins.input") as user_input, \
                self.assertRaisesRegex(SystemExit, "built-in Codex feature"):
            mcp_lens.remove_server(self.config, row)
        user_input.assert_not_called()
        self.assertEqual(self.config.read_text(encoding="utf-8"), original)

    def test_automatic_cleanup_never_offers_built_in_codex_feature(self):
        old_date = "2025-01-01 00:00 UTC"
        rows = [
            {"id": "plugin:codex-app-tools@openai-bundled:codex_app",
             "plugin_key": "codex-app-tools@openai-bundled",
             "last_used": old_date, "status": "Enabled by config"},
            {"id": "global:old", "plugin_key": None,
             "last_used": old_date, "status": "Enabled by config"},
        ]
        cutoff = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        self.assertEqual([row["id"] for row in mcp_lens.cleanup_candidates(rows, cutoff)],
                         ["global:old"])


if __name__ == "__main__":
    unittest.main()
