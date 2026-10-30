import datetime as dt
import contextlib
import io
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import tomllib
import urllib.error
import urllib.request
from unittest.mock import patch
import mcp_lens


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "mcp_lens.py"


class McpLensCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.home = self.root / ".codex"
        self.home.mkdir()
        self.config = self.home / "config.toml"
        self.config.write_text(
            '[mcp_servers.global-old]\n'
            'command = "python"\n'
            'enabled = true\n'
            '[mcp_servers.global-unknown]\n'
            'url = "https://example.invalid/mcp"\n'
            '[plugins."demo@local"]\n'
            'enabled = true\n'
            '[plugins."demo@local".mcp_servers.bundled]\n'
            'enabled_tools = ["read_note"]\n'
            'default_tools_approval_mode = "prompt"\n',
            encoding="utf-8",
        )
        manifest = self.home / "plugins/cache/local/demo/1.0.0/.mcp.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"mcpServers": {
            "bundled": {"command": "python", "enabled": True},
        }}), encoding="utf-8")
        now = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        old = now - 100 * 24 * 60 * 60 * 1000
        db = sqlite3.connect(self.home / "thread_history_1.sqlite")
        db.execute("CREATE TABLE thread_items (created_at_ms INTEGER, item_type TEXT, item_json TEXT)")
        calls = [
            (old - 10_000, "mcpToolCall", json.dumps({"server": "global-old", "status": "completed"})),
            (old, "mcpToolCall", json.dumps({"server": "global-old", "status": "failed", "error": {"message": "expected test failure"}})),
            (now - 60_000, "mcpToolCall", json.dumps({"server": "bundled", "status": "completed"})),
            (now - 30_000, "mcpToolCall", json.dumps({"server": "unconfigured", "status": "completed"})),
        ]
        db.executemany("INSERT INTO thread_items VALUES (?, ?, ?)", calls)
        db.commit()
        db.close()

    def run_cli(self, *args, input_text=None, live=False, extra_env=None):
        if args and args[0] == "report" and not live:
            args = ("report", "--offline", *args[1:])
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--home", str(self.home), *args],
            input=input_text, text=True, capture_output=True, check=False,
            env={**os.environ, "CODEX_HOME": str(self.home), **(extra_env or {})},
        )

    def test_runtime_reads_action_details_beyond_first_hundred_apps(self):
        fake_codex = self.root / "fake-codex"
        fake_codex.write_text(f"#!{sys.executable}\n" + '''
import json
import sys
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {}
    elif method == "app/installed":
        result = {"apps": [{"id": f"app-{n}", "enabled": True, "callable": True}
                           for n in range(101)]}
    elif method == "config/read":
        result = {"config": {}, "layers": []}
    elif method == "app/read":
        result = {"apps": [{"id": app_id, "toolSummaries": [
            {"name": "read", "isEnabled": True}]}
            for app_id in request["params"]["appIds"]]}
    elif method == "skills/list":
        result = {"data": [{"cwd": request["params"]["cwds"][0], "errors": [],
                            "skills": [{"name": "review", "description": "Review files",
                                        "enabled": True, "scope": "repo", "path": "/sample/SKILL.md"}]}]}
    elif method == "hooks/list":
        result = {"data": [{"cwd": request["params"]["cwds"][0], "errors": [],
                            "warnings": [], "hooks": [{"eventName": "preToolUse",
                                                       "handlerType": "command", "enabled": True,
                                                       "source": "project", "sourcePath": "/sample/hooks.json",
                                                       "command": "private command"}]}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"id": request["id"], "result": result}) + "\\n")
    sys.stdout.flush()
''', encoding="utf-8")
        fake_codex.chmod(0o700)
        with patch.object(mcp_lens.shutil, "which", return_value=str(fake_codex)):
            snapshot = mcp_lens.codex_runtime_snapshot(self.home, self.root)
        self.assertEqual(len(snapshot["apps"]), 101)
        self.assertEqual(len(snapshot["app_summaries"]), 101)
        self.assertIn("app-100", snapshot["app_summaries"])
        self.assertEqual(snapshot["skills_state"], "checked")
        self.assertEqual(snapshot["skills"][0]["name"], "review")
        self.assertEqual(snapshot["hooks_state"], "checked")
        self.assertEqual(snapshot["hooks"][0]["eventName"], "preToolUse")
        self.assertNotIn("command", snapshot["hooks"][0])

    def test_runtime_skips_invalid_discovery_folder_without_losing_app_status(self):
        fake_codex = self.root / "fake-codex-invalid-cwd"
        fake_codex.write_text(f"#!{sys.executable}\n" + '''
import json
import sys
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {}
    elif method == "app/installed":
        result = {"apps": [{"id": "notion", "enabled": True, "callable": True}]}
    elif method == "config/read":
        result = {"config": {}, "layers": []}
    elif method == "skills/list":
        result = {"data": [
            {"cwd": "\\u0000", "errors": [], "skills": []},
            {"cwd": request["params"]["cwds"][0], "errors": [],
             "skills": [{"name": "review", "enabled": True}]}]}
    elif method == "hooks/list":
        result = {"data": [{"cwd": "\\u0000", "errors": [], "warnings": [], "hooks": []}]}
    else:
        result = {"apps": []}
    sys.stdout.write(json.dumps({"id": request["id"], "result": result}) + "\\n")
    sys.stdout.flush()
''', encoding="utf-8")
        fake_codex.chmod(0o700)
        with patch.object(mcp_lens.shutil, "which", return_value=str(fake_codex)):
            snapshot = mcp_lens.codex_runtime_snapshot(self.home, self.root)
        self.assertEqual(snapshot["app_note"], mcp_lens.APP_STATUS_CHECKED_NOTE)
        self.assertTrue(snapshot["apps"]["notion"]["enabled"])
        self.assertEqual(snapshot["skills_state"], "checked")
        self.assertEqual(snapshot["skills"][0]["name"], "review")
        self.assertEqual(snapshot["hooks_state"], "unavailable")

    def test_report_explains_skills_hooks_and_permission_defaults_safely(self):
        report = self.root / "discovery.html"
        _, rows = mcp_lens.load_inventory(self.config, self.home)
        discovery = {
            "skills_state": "partial", "skills_errors": 1,
            "skills": [{"name": "Review", "description": "<img src=x onerror=alert(1)>",
                        "enabled": True, "scope": "repo", "path": "/example/SKILL.md"}],
            "hooks_state": "checked", "hooks_errors": 0, "hooks_warnings": 0,
            "hooks": [{"eventName": "preToolUse", "handlerType": "command",
                       "enabled": True, "source": "project",
                       "sourcePath": "/example/hooks.json", "trustStatus": "modified",
                       "command": "secret-token"}],
        }
        mcp_lens.render_html(
            rows, self.config, report, project_dir=self.root,
            discovery=discovery, permission_config={
                "sandbox_mode": "workspace-write",
                "approval_policy": {"granular": {"sandbox_approval": True}},
                "sandbox_workspace_write": {
                    "writable_roots": ["/example/<private>"], "network_access": False}},
            permissions_effective=True)
        page = report.read_text(encoding="utf-8")
        self.assertIn("Local file and command defaults", page)
        self.assertIn("Custom command-approval rules are configured", page)
        self.assertIn("Local command network access is set to off", page)
        self.assertIn("/example/&lt;private&gt;", page)
        self.assertIn("Codex listed 1 skill for this folder", page)
        self.assertIn("Codex reported 1 file error", page)
        self.assertIn("Before a tool is used", page)
        self.assertIn("Can run a local command", page)
        self.assertIn("Codex trust status: modified", page)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", page)
        self.assertNotIn("<img src=x onerror=alert(1)>", page)
        self.assertNotIn("secret-token", page)

    def test_unavailable_skill_and_hook_checks_are_not_shown_as_empty(self):
        section = mcp_lens.render_discovered_skills_and_hooks(None)
        self.assertIn("could not provide its skills list", section)
        self.assertIn("could not provide its hooks list", section)
        self.assertNotIn("listed 0 skills", section)

    def test_discovery_reports_partial_results_and_explains_known_hook_events(self):
        section = mcp_lens.render_discovered_skills_and_hooks({
            "skills_state": "partial", "skills_errors": 2, "skills": [],
            "hooks_state": "partial", "hooks_errors": 1, "hooks_warnings": 1,
            "hooks": [
                {"eventName": "preCompact", "handlerType": "prompt", "enabled": False,
                 "source": "sessionFlags"},
                {"eventName": "subagentStart", "handlerType": "agent", "enabled": True,
                 "source": "cloudManagedConfig"},
            ],
        })
        self.assertIn("Codex reported 2 file errors", section)
        self.assertIn("Codex reported 1 file error", section)
        self.assertIn("Codex also reported 1 warning", section)
        self.assertIn("Before a chat is shortened", section)
        self.assertIn("When a helper agent starts", section)
        self.assertIn("This session&#x27;s settings", section)
        self.assertIn("Managed workspace settings", section)
        self.assertNotIn("sessionFlags", section)

    def test_report_joins_user_and_plugin_mcp_history(self):
        report = self.root / "mcp-report.html"
        result = self.run_cli("report", "--output", str(report))
        self.assertEqual(result.returncode, 0, result.stderr)
        page = report.read_text(encoding="utf-8")
        self.assertIn("global-old", page)
        self.assertIn("bundled", page)
        self.assertIn("Unknown (Codex config has no install timestamp)", page)
        self.assertIn("(failed)", page)
        self.assertIn("1 calls; 1 completed", page)
        self.assertIn("No matching call record", page)
        self.assertIn("Apps and MCP servers found for Codex", page)
        self.assertIn("MCP servers", page)
        self.assertIn("Requests in the last 30 days", page)
        self.assertIn("No matching request in saved history checked", page)
        self.assertIn("How to read this report", page)
        self.assertIn("What can you do?", page)
        self.assertIn("Where did we find it?", page)
        self.assertIn(str(self.config), page)
        self.assertIn("mcpServers.bundled", page)
        self.assertIn("Use Turn off in Codex to keep this MCP server listed but off", page)
        self.assertIn("type /plugins in Codex", page)
        self.assertIn("body.manager-active .manage-button{display:inline-block}", page)
        self.assertIn("What this report can tell you about access", page)
        self.assertIn("See recorded activity", page)
        self.assertIn("Codex-side actions", page)
        self.assertIn("What can’t we confirm?", page)
        self.assertIn("<summary>More details</summary>", page)
        self.assertIn("Read 1 saved Codex history file", page)
        self.assertIn("no matching request does not prove an app or MCP server was never used", page)
        self.assertIn("Why review these entries?", page)
        self.assertIn("The triangle is a reminder to review the list", page)
        self.assertNotIn("No matching activity does not always mean", page)
        self.assertIn("Enabled by config (runtime unverified)", page)
        self.assertIn("default: prompt", page)
        self.assertNotIn("expected test failure", page)

    def test_action_guidance_matches_the_available_controls(self):
        direct = {"kind": "mcp", "plugin_key": None, "status": "Enabled in config (runtime unverified)"}
        plugin = {"kind": "mcp", "plugin_key": "demo@local", "status": "Enabled by config (runtime unverified)"}
        app = {"kind": "connector", "plugin_key": None, "status": "Disabled in config",
               "selected_folder_app_enabled": False}
        self.assertIn("Remove from Codex", mcp_lens.action_guidance(direct, True, True))
        self.assertNotIn("Use Turn off", mcp_lens.action_guidance(direct, False, True))
        self.assertIn("type /plugins in Codex", mcp_lens.action_guidance(plugin, True, False))
        scoped_plugin = {"kind": "mcp", "plugin_key": "@group/demo@local", "status": "Enabled"}
        self.assertIn("find @group/demo", mcp_lens.action_guidance(scoped_plugin, True, False))
        self.assertIn("already marked off", mcp_lens.action_guidance(app, False, False))

    def test_source_locations_include_project_layer_and_plugin_manifest(self):
        project_folder = self.root / "project" / ".codex"
        project_folder.mkdir(parents=True)
        layers = [{"name": {"type": "project", "dotCodexFolder": str(project_folder)},
                   "entries": {"mcp_servers": ["project-only"]}, "ignored": False}]
        rows = [{"kind": "mcp", "plugin_key": None, "name": "project-only"},
                {"kind": "mcp", "plugin_key": "demo@local", "name": "bundled",
                 "manifest_path": str(self.home / "plugins/cache/local/demo/1.0.0/.mcp.json")}]
        mcp_lens.annotate_source_locations(rows, self.config, layers)
        self.assertEqual(rows[0]["source_locations"], [{
            "label": "Codex settings entry", "path": str(project_folder / "config.toml"),
            "entry": "mcp_servers.project-only"}])
        self.assertEqual({location["entry"] for location in rows[1]["source_locations"]},
                         {"plugins.demo@local", "mcpServers.bundled"})

    def test_source_locations_do_not_claim_a_file_when_unidentified(self):
        rows = [{"kind": "connector", "plugin_key": None, "name": "Unknown app",
                 "app_slug": "unknown_app", "connector_id": None}]
        mcp_lens.annotate_source_locations(rows, self.config, [])
        self.assertEqual(rows[0]["source_locations"], [])

    def test_file_edit_history_is_limited_to_chosen_folder_and_deduplicated(self):
        selected = self.root / "project"
        lookalike = self.root / "project-copy"
        selected.mkdir()
        lookalike.mkdir()
        now = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        db = sqlite3.connect(self.home / "thread_history_1.sqlite")
        item = {"type": "fileChange", "id": "edit-1", "status": "completed",
                "changes": [{"path": str(selected / "note.txt"), "kind": {"type": "update"}}]}
        ignored = {"type": "fileChange", "id": "edit-2", "status": "declined",
                   "changes": [{"path": str(selected / "secret.txt"), "kind": {"type": "add"}}]}
        outside = {"type": "fileChange", "id": "edit-3", "status": "completed",
                   "changes": [{"path": str(lookalike / "other.txt"), "kind": {"type": "add"}}]}
        db.executemany("INSERT INTO thread_items VALUES (?, ?, ?)",
                       [(now, "fileChange", json.dumps(value)) for value in (item, item, ignored, outside)])
        db.commit()
        db.close()
        found = mcp_lens.folder_file_changes(self.home, selected)
        self.assertEqual((found["count"], found["files"]), (1, 1))
        self.assertEqual(found["events"][0][1:], ("note.txt", "Edited"))
        report = self.root / "folder-report.html"
        result = self.run_cli("report", "--project", str(selected), "--output", str(report))
        self.assertEqual(result.returncode, 0, result.stderr)
        page = report.read_text(encoding="utf-8")
        self.assertIn("File edits recorded by Codex in this folder", page)
        self.assertIn("note.txt", page)
        self.assertNotIn("secret.txt", page)
        self.assertNotIn("other.txt", page)

    def test_file_edit_history_uses_session_record_when_database_has_none(self):
        selected = self.root / "project"
        selected.mkdir()
        sessions = self.home / "sessions"
        sessions.mkdir()
        session = sessions / "sample.jsonl"
        events = [
            {"type": "session_meta", "payload": {"id": "thread-1", "cwd": str(selected)}},
            {"type": "event_msg", "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
             "payload": {"type": "item_completed", "item": {"type": "FileChange", "id": "edit-4",
                         "status": "completed", "changes": [{"path": "new.txt", "kind": {"type": "add"}}]}}},
        ]
        session.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        found = mcp_lens.folder_file_changes(self.home, selected)
        self.assertEqual(found["count"], 1)
        self.assertEqual(found["events"][0][1:], ("new.txt", "Added"))

    def test_malformed_file_edit_copy_does_not_hide_valid_copy(self):
        selected = self.root / "project"
        selected.mkdir()
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        db = sqlite3.connect(self.home / "thread_history_1.sqlite")
        db.executemany("INSERT INTO thread_items VALUES (?, ?, ?)", [
            (stamp, "fileChange", json.dumps({"id": "same-edit", "status": "completed",
                                               "changes": "invalid"})),
            (stamp, "fileChange", json.dumps({"id": "same-edit", "status": "completed",
                                               "changes": [{"path": str(selected / "note.txt"),
                                                            "kind": {"type": "update"}}]})),
        ])
        db.commit()
        db.close()
        found = mcp_lens.folder_file_changes(self.home, selected)
        self.assertEqual(found["count"], 1)
        self.assertEqual(found["events"][0][1:], ("note.txt", "Edited"))
        self.assertEqual(found["coverage"]["records_unreadable"], 1)

    def test_report_is_private_and_does_not_follow_output_symlink(self):
        report = self.root / "private-report.html"
        first = self.run_cli("report", "--output", str(report))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)

        report.chmod(0o644)
        second = self.run_cli("report", "--output", str(report))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)

        target = self.root / "unrelated-file.txt"
        target.write_text("do not overwrite", encoding="utf-8")
        report.unlink()
        report.symlink_to(target)
        third = self.run_cli("report", "--output", str(report))
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), "do not overwrite")
        self.assertFalse(report.is_symlink())
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)

    def test_report_escapes_names_from_local_configuration(self):
        self.config.write_text(
            '[mcp_servers."<img src=x onerror=alert(1)>"]\ncommand = "python"\n',
            encoding="utf-8",
        )
        report = self.root / "escaped-report.html"
        result = self.run_cli("report", "--output", str(report))
        self.assertEqual(result.returncode, 0, result.stderr)
        page = report.read_text(encoding="utf-8")
        self.assertNotIn("<img src=x onerror=alert(1)>", page)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", page)

    def test_share_export_contains_counts_without_names_or_paths(self):
        report = self.root / "private-report.html"
        share = self.root / "share.html"
        result = self.run_cli("report", "--output", str(report), "--share-output", str(share))
        self.assertEqual(result.returncode, 0, result.stderr)
        page = share.read_text(encoding="utf-8")
        self.assertIn("MCP Lens summary", page)
        self.assertIn("History files checked", page)
        self.assertNotIn("global-old", page)
        self.assertNotIn("bundled", page)
        self.assertNotIn(str(self.home), page)
        self.assertNotIn("example.invalid", page)
        self.assertEqual(share.stat().st_mode & 0o777, 0o600)

    def test_demo_report_uses_only_fictional_data(self):
        demo = self.root / "demo.html"
        result = self.run_cli("demo", "--output", str(demo))
        self.assertEqual(result.returncode, 0, result.stderr)
        page = demo.read_text(encoding="utf-8")
        self.assertIn("Sample only", page)
        self.assertIn("Maple Notes", page)
        self.assertIn("Photo Shelf", page)
        self.assertIn("<small>App (connector)</small>", page)
        self.assertIn("<small>MCP server</small>", page)
        self.assertIn("<h2 id=\"terms-title\">What is listed here?</h2>", page)
        self.assertLess(page.index("Apps (connectors) with recorded requests"),
                        page.index("Other apps (connectors) on in Codex's general list"))
        self.assertIn("Latest recorded requests", page)
        self.assertNotIn(str(self.home), page)
        self.assertNotIn("global-old", page)
        self.assertNotIn("mcp-lens-demo-", page)
        self.assertEqual(demo.stat().st_mode & 0o777, 0o600)

    def test_timeline_does_not_show_request_contents(self):
        db = sqlite3.connect(self.home / "thread_history_1.sqlite")
        db.execute("INSERT INTO thread_items VALUES (?, ?, ?)", (
            int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000), "mcpToolCall",
            json.dumps({"id": "private-call", "server": "global-old", "tool": "search",
                        "status": "completed", "arguments": {"query": "VERY_PRIVATE_QUERY"},
                        "result": {"text": "VERY_PRIVATE_RESULT"}})))
        db.commit()
        db.close()
        report = self.root / "private-timeline.html"
        self.assertEqual(self.run_cli("report", "--output", str(report)).returncode, 0)
        page = report.read_text(encoding="utf-8")
        self.assertIn("Latest recorded requests", page)
        self.assertNotIn("VERY_PRIVATE_QUERY", page)
        self.assertNotIn("VERY_PRIVATE_RESULT", page)

    def test_report_cannot_overwrite_settings_or_reuse_snapshot_path(self):
        original = self.config.read_text(encoding="utf-8")
        settings_target = self.run_cli("report", "--output", str(self.config))
        self.assertNotEqual(settings_target.returncode, 0)
        self.assertEqual(self.config.read_text(encoding="utf-8"), original)
        shared_path = self.root / "same.html"
        same_destination = self.run_cli("report", "--output", str(shared_path),
                                        "--share-output", str(shared_path))
        self.assertNotEqual(same_destination.returncode, 0)
        self.assertFalse(shared_path.exists())

    def test_session_log_calls_missing_from_database_are_counted(self):
        session = self.home / "sessions/2026/10/24/example.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(json.dumps({
            "type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "McpToolCall", "id": "missing-call-id", "server": "global-unknown",
                "tool": "read", "status": "completed"}}
        }) + "\n", encoding="utf-8")
        coverage = {}
        _, rows = mcp_lens.load_inventory(self.config, self.home, coverage)
        self.assertEqual(coverage["session_calls_missing"], 1)
        self.assertEqual(coverage["session_calls_added"], 1)
        unknown = next(row for row in rows if row["name"] == "global-unknown")
        self.assertEqual(unknown["history_scan_state"], "checked")
        self.assertEqual(unknown["use_count"], 1)
        report = self.root / "missing-call-report.html"
        result = self.run_cli("report", "--output", str(report))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Added 1 call from session logs", report.read_text(encoding="utf-8"))

    def test_session_only_call_uses_session_starting_folder(self):
        (self.home / "thread_history_1.sqlite").unlink()
        selected = self.root / "selected"
        selected.mkdir()
        session = self.home / "sessions/2026/10/24/example.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text("\n".join(json.dumps(event) for event in [
            {"type": "session_meta", "payload": {"id": "session-one", "cwd": str(selected)}},
            {"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(), "type": "event_msg",
             "payload": {"type": "item_completed", "item": {
                 "type": "McpToolCall", "id": "session-call", "server": "global-old",
                 "tool": "read", "status": "completed"}}},
        ]) + "\n", encoding="utf-8")
        coverage = {}
        _, rows = mcp_lens.load_inventory(self.config, self.home, coverage, project_dir=selected)
        row = next(r for r in rows if r["name"] == "global-old")
        self.assertEqual(row["use_count"], 1)
        self.assertEqual(row["recent_count"], 1)
        self.assertEqual(row["selected_folder_count"], 1)
        self.assertEqual(row["unknown_folder_count"], 0)
        self.assertEqual(row["history_scan_state"], "partial")

    def test_malformed_session_call_does_not_hide_valid_copy(self):
        (self.home / "thread_history_1.sqlite").unlink()
        session = self.home / "sessions/example.jsonl"
        session.parent.mkdir()
        invalid = {"type": "McpToolCall", "id": "same-call", "tool": "read"}
        valid = {**invalid, "server": "global-old"}
        session.write_text("\n".join(json.dumps({
            "type": "event_msg", "payload": {"type": "item_completed", "item": item}})
            for item in (invalid, valid)) + "\n", encoding="utf-8")
        coverage = {}
        usage = mcp_lens.history_usage(self.home, coverage)
        self.assertEqual(usage["global-old"]["count"], 1)
        self.assertEqual(coverage["session_calls_added"], 1)
        self.assertEqual(coverage["session_calls_missing"], 1)
        self.assertEqual(coverage["session_records_unreadable"], 1)

    def test_same_call_in_history_and_session_is_not_counted_twice(self):
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        db = sqlite3.connect(self.home / "thread_history_1.sqlite")
        db.execute("INSERT INTO thread_items VALUES (?, ?, ?)", (
            stamp, "mcpToolCall", json.dumps({"id": "shared-call", "server": "global-old", "tool": "read"})))
        db.commit()
        db.close()
        session = self.home / "sessions/2026/10/24/example.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(json.dumps({"type": "event_msg", "payload": {"type": "item_completed",
            "item": {"type": "McpToolCall", "id": "shared-call", "server": "global-old", "tool": "read"}}}) + "\n",
            encoding="utf-8")
        coverage = {}
        usage = mcp_lens.history_usage(self.home, coverage)
        self.assertEqual(usage["global-old"]["count"], 3)
        self.assertEqual(coverage["session_calls_added"], 0)

    def test_future_dated_session_call_is_not_recent(self):
        (self.home / "thread_history_1.sqlite").unlink()
        session = self.home / "sessions/2026/10/24/example.jsonl"
        session.parent.mkdir(parents=True)
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)
        session.write_text(json.dumps({
            "timestamp": future.isoformat(), "type": "event_msg",
            "payload": {"type": "item_completed", "completed_at_ms": int(future.timestamp() * 1000),
                        "item": {"type": "McpToolCall", "id": "future-session-call",
                                 "server": "global-old", "tool": "read"}}}) + "\n", encoding="utf-8")
        coverage = {}
        usage = mcp_lens.history_usage(self.home, coverage)
        self.assertEqual(usage["global-old"]["count"], 1)
        self.assertEqual(usage["global-old"]["recent_count"], 0)
        self.assertEqual(coverage["records_undated"], 1)

    def test_conflicting_saved_starting_folders_are_unknown(self):
        selected = self.root / "selected"
        other = self.root / "other"
        selected.mkdir()
        other.mkdir()
        for version, folder in ((1, selected), (2, other)):
            state = sqlite3.connect(self.home / f"state_{version}.sqlite")
            state.execute("CREATE TABLE threads (id TEXT, cwd TEXT)")
            state.execute("INSERT INTO threads VALUES (?, ?)", ("shared-thread", str(folder)))
            state.commit()
            state.close()
        db = sqlite3.connect(self.home / "thread_history_1.sqlite")
        db.execute("ALTER TABLE thread_items ADD COLUMN thread_id TEXT")
        db.execute("INSERT INTO thread_items (created_at_ms, item_type, item_json, thread_id) VALUES (?, ?, ?, ?)",
                   (int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000), "mcpToolCall",
                    json.dumps({"server": "global-old", "tool": "read"}), "shared-thread"))
        db.commit()
        db.close()
        coverage = {}
        _, rows = mcp_lens.load_inventory(self.config, self.home, coverage, project_dir=selected)
        row = next(r for r in rows if r["name"] == "global-old")
        self.assertEqual(coverage["folder_conflicts"], 1)
        self.assertEqual(row["selected_folder_count"], 0)
        self.assertEqual(row["other_folder_count"], 0)
        self.assertEqual(row["unknown_folder_count"], 3)

    def test_calls_are_split_by_known_starting_folder(self):
        selected = self.root / "selected"
        other = self.root / "other"
        selected.mkdir()
        other.mkdir()
        state = sqlite3.connect(self.home / "state_5.sqlite")
        state.execute("CREATE TABLE threads (id TEXT, cwd TEXT)")
        state.executemany("INSERT INTO threads VALUES (?, ?)",
                          [("thread-selected", str(selected)), ("thread-other", str(other))])
        state.commit()
        state.close()
        db = sqlite3.connect(self.home / "thread_history_1.sqlite")
        db.execute("ALTER TABLE thread_items ADD COLUMN thread_id TEXT")
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        for thread_id in ("thread-selected", "thread-other"):
            db.execute("INSERT INTO thread_items (created_at_ms, item_type, item_json, thread_id) VALUES (?, ?, ?, ?)",
                       (stamp, "mcpToolCall", json.dumps({"server": "global-old", "tool": "read"}), thread_id))
        db.commit()
        db.close()
        coverage = {}
        _, rows = mcp_lens.load_inventory(self.config, self.home, coverage, project_dir=selected)
        row = next(r for r in rows if r["name"] == "global-old")
        self.assertEqual((row["selected_folder_count"], row["other_folder_count"],
                          row["unknown_folder_count"]), (1, 1, 2))
        self.assertEqual(row["use_count"], 4)

    def test_duplicate_call_ids_are_counted_once(self):
        db = sqlite3.connect(self.home / "thread_history_1.sqlite")
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        call = json.dumps({"id": "repeat-id", "server": "global-old", "tool": "read", "status": "completed"})
        db.executemany("INSERT INTO thread_items VALUES (?, ?, ?)", [
            (stamp, "mcpToolCall", call), (stamp, "mcpToolCall", call),
        ])
        db.commit()
        db.close()
        coverage = {}
        usage = mcp_lens.history_usage(self.home, coverage)
        self.assertEqual(coverage["records_duplicate"], 1)
        self.assertEqual(usage["global-old"]["count"], 3)

    def test_cleanup_does_not_emit_control_characters_from_server_names(self):
        self.config.write_text(
            '[mcp_servers."\\u001b[2J"]\ncommand = "python"\n',
            encoding="utf-8",
        )
        db = sqlite3.connect(self.home / "thread_history_1.sqlite")
        old = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=100)).timestamp() * 1000)
        db.execute("INSERT INTO thread_items VALUES (?, ?, ?)",
                   (old, "mcpToolCall", json.dumps({"server": "\x1b[2J", "tool": "read"})))
        db.commit()
        db.close()
        result = self.run_cli("cleanup", "--older-than", "90")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\x1b", result.stdout)
        self.assertIn("\\x1b[2J", result.stdout)

    def test_malformed_history_rows_do_not_crash_or_claim_no_activity(self):
        self.config.write_text(self.config.read_text(encoding="utf-8") +
                               '\n[mcp_servers.untimed]\ncommand = "python"\n', encoding="utf-8")
        db = sqlite3.connect(self.home / "thread_history_1.sqlite")
        db.executemany("INSERT INTO thread_items VALUES (?, ?, ?)", [
            ("not-a-time", "mcpToolCall", json.dumps({"server": "untimed", "tool": "inspect"})),
            (0, "mcpToolCall", "[]"),
        ])
        db.commit()
        db.close()
        report = self.root / "malformed-history.html"
        result = self.run_cli("report", "--output", str(report))
        self.assertEqual(result.returncode, 0, result.stderr)
        page = report.read_text(encoding="utf-8")
        self.assertIn("Codex made 1 recorded request, but their dates are unavailable", page)

    def test_malformed_tool_filter_does_not_stop_report(self):
        self.config.write_text(
            '[mcp_servers.broken]\ncommand = "python"\n'
            'allowed_tools = { tool_names = 23 }\n', encoding="utf-8")
        report = self.root / "malformed-filter.html"
        result = self.run_cli("report", "--output", str(report))
        self.assertEqual(result.returncode, 0, result.stderr)
        page = report.read_text(encoding="utf-8")
        self.assertIn("broken", page)
        self.assertIn("Policy filter configured", page)

    def test_report_shows_mcp_controls_without_leaking_values(self):
        # Add controls to a valid server table without exposing secret values.
        text = self.config.read_text(encoding="utf-8").replace(
            '[mcp_servers.global-old]\ncommand = "python"\nenabled = true\n',
            '[mcp_servers.global-old]\ncommand = "python"\nenabled = true\n'
            'disabled_tools = ["delete_file"]\nscopes = ["read:files"]\n'
            'bearer_token_env_var = "MCP_TOKEN"\nhttp_headers = { Authorization = "Bearer header-secret" }\n'
            'env = { TOKEN = "secret-value" }\n')
        self.config.write_text(text, encoding="utf-8")
        report = self.root / "safe-settings.html"
        result = self.run_cli("report", "--output", str(report))
        self.assertEqual(result.returncode, 0, result.stderr)
        page = report.read_text(encoding="utf-8")
        self.assertIn("Tools explicitly disabled", page)
        self.assertIn("Requested OAuth scopes", page)
        self.assertIn("MCP_TOKEN", page)
        self.assertNotIn("secret-value", page)
        self.assertNotIn("Bearer header-secret", page)

    def test_codex_app_tools_are_explained_without_claiming_files_were_audited(self):
        plugin = 'codex-app-tools@openai-bundled'
        self.config.write_text(
            f'[plugins."{plugin}"]\nenabled = false\n'
            f'[plugins."{plugin}".mcp_servers.codex_app]\n'
            'enabled = false\ndefault_tools_approval_mode = "approve"\n'
            f'[plugins."{plugin}".mcp_servers.codex_app.tools.open_in_codex]\n'
            'approval_mode = "prompt"\n', encoding="utf-8"
        )
        manifest = self.home / "plugins/cache/openai-bundled/codex-app-tools/0.1.6/.mcp.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"mcpServers": {
            "codex_app": {"command": "codex_app", "enabled": True}
        }}), encoding="utf-8")
        now = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        db = sqlite3.connect(self.home / "thread_history_1.sqlite")
        db.executemany("INSERT INTO thread_items VALUES (?, ?, ?)", [
            (now, "mcpToolCall", json.dumps({"server": "codex_app", "tool": "open_in_codex", "status": "completed",
                "arguments": {"target": {"type": "file", "path": "/Users/person/Private/Finance.xlsx"}}})),
            (now, "mcpToolCall", json.dumps({"server": "codex_app", "tool": "load_workspace_dependencies", "status": "completed",
                "arguments": {}})),
        ])
        db.commit()
        db.close()

        report = self.root / "codex-app-report.html"
        result = self.run_cli("report", "--output", str(report))
        self.assertEqual(result.returncode, 0, result.stderr)
        page = report.read_text(encoding="utf-8")
        self.assertIn("Codex features", page)
        self.assertIn("Built-in Codex feature", page)
        self.assertIn("Marked off in settings; past requests remain in history", page)
        self.assertIn("asked Codex to open 1 file ", page)
        self.assertIn("Finance.xlsx", page)
        self.assertNotIn("/Users/person/Private", page)
        self.assertIn("does not prove its contents were reviewed", page)
        self.assertIn("History shows 2 kinds of action", page)
        self.assertIn("Separate confirmation rules are set for 1 action", page)
        self.assertIn("Saved requests may have happened before that setting changed", page)

    def test_invalid_manifest_json_shapes_are_ignored(self):
        bad = self.home / "plugins/cache/local/bad/1.0.0"
        bad.mkdir(parents=True)
        (bad / ".mcp.json").write_text("[]", encoding="utf-8")
        (bad / ".app.json").write_text("null", encoding="utf-8")
        report = self.root / "bad-manifest.html"
        result = self.run_cli("report", "--output", str(report))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("bad@local", report.read_text(encoding="utf-8"))

    def test_cleanup_dry_run_does_not_mutate_and_apply_removes_without_backup(self):
        original = self.config.read_text(encoding="utf-8")
        dry = self.run_cli("cleanup", "--older-than", "90")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("global:global-old", dry.stdout)
        self.assertIn("No matching dated request found: 1 (not a cleanup candidate)", dry.stdout)
        self.assertEqual(self.config.read_text(encoding="utf-8"), original)
        self.assertFalse(list(self.home.glob("config.toml.backup-*")))

        applied = self.run_cli("cleanup", "--older-than", "90", "--apply", input_text="global:global-old\n")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        parsed = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertNotIn("global-old", parsed["mcp_servers"])
        self.assertIn("global-unknown", parsed["mcp_servers"])
        self.assertFalse(list(self.home.glob("config.toml.backup-*")))

    def test_plugin_cleanup_adds_disabled_override_not_package_edit(self):
        manifest = self.home / "plugins/cache/local/demo/1.0.0/.mcp.json"
        original_manifest = manifest.read_text(encoding="utf-8")
        result = self.run_cli("remove", "plugin:demo@local:bundled", input_text="plugin:demo@local:bundled\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertFalse(parsed["plugins"]["demo@local"]["mcp_servers"]["bundled"]["enabled"])
        self.assertEqual(manifest.read_text(encoding="utf-8"), original_manifest)
        self.assertFalse(list(self.home.glob("config.toml.backup-*")))

    def test_manage_actions_change_only_selected_settings_without_backup(self):
        _, rows = mcp_lens.load_inventory(self.config, self.home)
        plugin = next(row for row in rows if row["id"] == "plugin:demo@local:bundled")
        self.assertIn("switched off", mcp_lens.disable_entry(self.config, plugin))
        parsed = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertFalse(parsed["plugins"]["demo@local"]["mcp_servers"]["bundled"]["enabled"])

        app = {"kind": "connector", "app_slug": "sample_notes", "plugin_key": "sample@local"}
        self.assertIn("switched off", mcp_lens.disable_entry(self.config, app))
        parsed = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertFalse(parsed["apps"]["sample_notes"]["enabled"])

        direct = next(row for row in rows if row["id"] == "global:global-old")
        self.assertIn("removed", mcp_lens.remove_direct_mcp_entry(self.config, direct))
        parsed = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertNotIn("global-old", parsed["mcp_servers"])
        self.assertIn("global-unknown", parsed["mcp_servers"])
        self.assertFalse(list(self.home.glob("config.toml.backup-*")))

    def test_app_disable_uses_existing_connector_id_setting_when_slug_is_absent(self):
        self.config.write_text('[apps."app-notion"]\nenabled = true\n', encoding="utf-8")
        row = {"kind": "connector", "app_slug": "notion", "connector_id": "app-notion"}
        mcp_lens.disable_entry(self.config, row)
        apps = tomllib.loads(self.config.read_text(encoding="utf-8"))["apps"]
        self.assertFalse(apps["app-notion"]["enabled"])
        self.assertNotIn("notion", apps)

    def test_config_only_app_is_listed_without_claiming_account_connection(self):
        self.config.write_text(self.config.read_text(encoding="utf-8") +
                               '\n[apps.canva]\nenabled = false\n', encoding="utf-8")
        report = self.root / "config-app.html"
        result = self.run_cli("report", "--output", str(report))
        self.assertEqual(result.returncode, 0, result.stderr)
        page = report.read_text(encoding="utf-8")
        self.assertIn('<strong>Canva</strong><small>App (connector)</small>', page)
        self.assertIn('This app has a saved Codex setting', page)
        self.assertIn('No saved action list was found for this app.', page)
        self.assertIn('To disconnect the outside account', page)
        self.assertIn('This app is already marked off in the Codex settings checked.', page)
        self.assertEqual(page.count('href="https://chatgpt.com/plugins"'), 1)
        _, rows = mcp_lens.load_inventory(self.config, self.home)
        self.assertEqual(len([row for row in rows if row.get("app_slug") == "canva"]), 1)

    def test_app_setting_does_not_duplicate_plugin_app(self):
        self.config.write_text('[plugins."notes@local"]\nenabled = true\n'
                               '[apps.notes]\nenabled = false\n', encoding="utf-8")
        manifest = self.home / "plugins/cache/local/notes/1.0.0/.app.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"apps": {"notes": {"id": "app-notes"}}}), encoding="utf-8")
        _, rows = mcp_lens.load_inventory(self.config, self.home)
        self.assertEqual(len([row for row in rows if row.get("kind") == "connector"]), 1)

    def test_selected_folder_app_setting_takes_precedence_in_report_status(self):
        row = {"kind": "connector", "app_slug": "canva", "connector_id": "app-canva",
               "runtime_app": {"enabled": True, "callable": True}}
        effective = {"apps": {"canva": {"enabled": False}}}
        row["selected_folder_app_enabled"] = mcp_lens.app_enabled_for_selected_folder(effective, row)
        self.assertEqual(mcp_lens.friendly_status(row), "Off in Codex settings for this folder")
        effective["apps"]["canva"]["enabled"] = True
        row["selected_folder_app_enabled"] = mcp_lens.app_enabled_for_selected_folder(effective, row)
        row["runtime_app"] = {"enabled": False, "callable": False}
        self.assertIn("off in Codex's general app list", mcp_lens.friendly_status(row))

    def test_local_manager_rejects_foreign_origin_and_verifies_disable(self):
        report = self.root / "private-report.html"
        made = self.run_cli("report", "--output", str(report))
        self.assertEqual(made.returncode, 0, made.stderr)
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--home", str(self.home), "manage", "--no-open",
             "--project", str(self.root), "--report", str(report)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "PATH": "", "CODEX_HOME": str(self.home)},
        )
        def stop_manager():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.addCleanup(stop_manager)
        self.assertIn("running on this computer", process.stdout.readline())
        url = process.stdout.readline().strip().split(": ", 1)[1]
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        page = urllib.request.urlopen(url, timeout=5).read().decode("utf-8")
        self.assertIn('data-action="disable"', page)
        origin = url.split("/s/", 1)[0]
        endpoint = url.rsplit("/report", 1)[0] + "/action"
        payload = json.dumps({"id": "global:global-old", "action": "disable"}).encode()
        foreign = urllib.request.Request(endpoint, data=payload, method="POST",
                                         headers={"Origin": "https://example.invalid", "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(foreign, timeout=5)
        self.assertEqual(rejected.exception.code, 403)
        rejected.exception.close()
        self.assertTrue(tomllib.loads(self.config.read_text())["mcp_servers"]["global-old"]["enabled"])

        allowed = urllib.request.Request(endpoint, data=payload, method="POST",
                                         headers={"Origin": origin, "Content-Type": "application/json"})
        result = json.loads(urllib.request.urlopen(allowed, timeout=20).read())
        self.assertTrue(result["saved"])
        self.assertTrue(result["refreshed"])
        self.assertTrue(result["verified"])
        self.assertFalse(tomllib.loads(self.config.read_text())["mcp_servers"]["global-old"]["enabled"])
        self.assertFalse(list(self.home.glob("config.toml.backup-*")))

        remove_payload = json.dumps({"id": "global:global-old", "action": "remove"}).encode()
        remove_request = urllib.request.Request(endpoint, data=remove_payload, method="POST",
                                                headers={"Origin": origin, "Content-Type": "application/json"})
        removed = json.loads(urllib.request.urlopen(remove_request, timeout=20).read())
        self.assertTrue(removed["saved"])
        self.assertTrue(removed["verified"])
        self.assertNotIn("global-old", tomllib.loads(self.config.read_text())["mcp_servers"])
        self.assertFalse(list(self.home.glob("config.toml.backup-*")))

    def test_local_manager_never_serves_supplied_or_replaced_html(self):
        report = self.root / "private-report.html"
        malicious = "<html><script>window.untrusted_report_marker = true</script></html>"
        report.write_text(malicious, encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--home", str(self.home), "manage", "--no-open",
             "--project", str(self.root), "--report", str(report)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "PATH": "", "CODEX_HOME": str(self.home)},
        )
        def stop_manager():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.addCleanup(stop_manager)
        self.assertIn("running on this computer", process.stdout.readline())
        url = process.stdout.readline().strip().split(": ", 1)[1]
        trusted_page = urllib.request.urlopen(url, timeout=5).read().decode("utf-8")
        self.assertIn('data-action="disable"', trusted_page)
        self.assertNotIn("untrusted_report_marker", trusted_page)
        self.assertEqual(report.read_text(encoding="utf-8"), malicious)

        report.write_text(malicious, encoding="utf-8")
        later_page = urllib.request.urlopen(url, timeout=5).read().decode("utf-8")
        self.assertEqual(later_page, trusted_page)
        self.assertTrue(tomllib.loads(self.config.read_text())["mcp_servers"]["global-old"]["enabled"])

    def test_local_manager_turns_off_app_and_reports_verified_state(self):
        self.config.write_text('[plugins."notes@local"]\nenabled = true\n', encoding="utf-8")
        manifest = self.home / "plugins/cache/local/notes/1.0.0/.app.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"apps": {"sample_notes": {"id": "sample-notes"}}}), encoding="utf-8")
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake_codex = fake_bin / "codex"
        fake_codex.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys, tomllib\n"
            "for line in sys.stdin:\n"
            " request = json.loads(line)\n"
            " config = tomllib.loads((pathlib.Path(os.environ['CODEX_HOME']) / 'config.toml').read_text())\n"
            " off = config.get('apps', {}).get('sample_notes', {}).get('enabled') is False\n"
            " rid = request.get('id')\n"
            " if rid == 1: result = {}\n"
            " elif rid == 2: result = {'apps': [{'id': 'sample-notes', 'enabled': not off, 'callable': not off}]}\n"
            " elif rid == 3: result = {'config': config, 'layers': []}\n"
            " elif rid == 4: result = {'apps': []}\n"
            " else: result = {}\n"
            " print(json.dumps({'id': rid, 'result': result}), flush=True)\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)
        report = self.root / "app-report.html"
        made = self.run_cli("report", "--output", str(report))
        self.assertEqual(made.returncode, 0, made.stderr)
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--home", str(self.home), "manage", "--no-open",
             "--project", str(self.root), "--report", str(report)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "PATH": str(fake_bin), "CODEX_HOME": str(self.home)},
        )
        def stop_manager():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.addCleanup(stop_manager)
        self.assertIn("running on this computer", process.stdout.readline())
        url = process.stdout.readline().strip().split(": ", 1)[1]
        endpoint = url.rsplit("/report", 1)[0] + "/action"
        origin = url.split("/s/", 1)[0]
        payload = json.dumps({"id": "connector:notes@local:sample_notes", "action": "disable"}).encode()
        request = urllib.request.Request(endpoint, data=payload, method="POST",
                                         headers={"Origin": origin, "Content-Type": "application/json"})
        result = json.loads(urllib.request.urlopen(request, timeout=25).read())
        self.assertTrue(result["saved"])
        self.assertTrue(result["refreshed"])
        self.assertTrue(result["verified"])
        self.assertFalse(tomllib.loads(self.config.read_text())["apps"]["sample_notes"]["enabled"])
        self.assertFalse(list(self.home.glob("config.toml.backup-*")))

    def test_local_manager_does_not_verify_app_off_when_folder_override_stays_on(self):
        self.config.write_text('[plugins."notes@local"]\nenabled = true\n', encoding="utf-8")
        manifest = self.home / "plugins/cache/local/notes/1.0.0/.app.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"apps": {"sample_notes": {"id": "sample-notes"}}}), encoding="utf-8")
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake_codex = fake_bin / "codex"
        fake_codex.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "for line in sys.stdin:\n"
            " request = json.loads(line)\n"
            " rid = request.get('id')\n"
            " if rid == 1: result = {}\n"
            " elif rid == 2: result = {'apps': [{'id': 'sample-notes', 'enabled': False, 'callable': False}]}\n"
            " elif rid == 3: result = {'config': {'plugins': {'notes@local': {'enabled': True}}, "
            "'apps': {'sample_notes': {'enabled': True}}}, 'layers': []}\n"
            " elif rid == 4: result = {'apps': []}\n"
            " else: result = {}\n"
            " print(json.dumps({'id': rid, 'result': result}), flush=True)\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)
        report = self.root / "app-report.html"
        made = self.run_cli("report", "--output", str(report))
        self.assertEqual(made.returncode, 0, made.stderr)
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--home", str(self.home), "manage", "--no-open",
             "--project", str(self.root), "--report", str(report)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "PATH": str(fake_bin), "CODEX_HOME": str(self.home)},
        )
        def stop_manager():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.addCleanup(stop_manager)
        self.assertIn("running on this computer", process.stdout.readline())
        url = process.stdout.readline().strip().split(": ", 1)[1]
        endpoint = url.rsplit("/report", 1)[0] + "/action"
        origin = url.split("/s/", 1)[0]
        payload = json.dumps({"id": "connector:notes@local:sample_notes", "action": "disable"}).encode()
        request = urllib.request.Request(endpoint, data=payload, method="POST",
                                         headers={"Origin": origin, "Content-Type": "application/json"})
        result = json.loads(urllib.request.urlopen(request, timeout=25).read())
        self.assertTrue(result["saved"])
        self.assertTrue(result["refreshed"])
        self.assertFalse(result["verified"])
        self.assertFalse(tomllib.loads(self.config.read_text())["apps"]["sample_notes"]["enabled"])

    def test_local_manager_reports_saved_change_when_refresh_fails(self):
        report = self.root / "report.html"
        made = self.run_cli("report", "--output", str(report))
        self.assertEqual(made.returncode, 0, made.stderr)
        blocked_share = self.root / "blocked-share.html"
        blocked_share.mkdir()
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--home", str(self.home), "manage", "--no-open",
             "--project", str(self.root), "--report", str(report),
             "--share-output", str(blocked_share)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "PATH": "", "CODEX_HOME": str(self.home)},
        )
        def stop_manager():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.addCleanup(stop_manager)
        self.assertIn("running on this computer", process.stdout.readline())
        url = process.stdout.readline().strip().split(": ", 1)[1]
        page_before = urllib.request.urlopen(url, timeout=5).read().decode("utf-8")
        origin = url.split("/s/", 1)[0]
        endpoint = url.rsplit("/report", 1)[0] + "/action"
        payload = json.dumps({"id": "global:global-old", "action": "disable"}).encode()
        request = urllib.request.Request(endpoint, data=payload, method="POST",
                                         headers={"Origin": origin, "Content-Type": "application/json"})
        result = json.loads(urllib.request.urlopen(request, timeout=20).read())
        self.assertTrue(result["saved"])
        self.assertTrue(result["refreshed"])
        self.assertTrue(result["verified"])
        self.assertIn("counts-only summary could not refresh", result["message"])
        page_after = urllib.request.urlopen(url, timeout=5).read().decode("utf-8")
        self.assertNotEqual(page_after, page_before)
        self.assertIn("Disabled in config", page_after)
        self.assertFalse(tomllib.loads(self.config.read_text())["mcp_servers"]["global-old"]["enabled"])

    def test_wrong_confirmation_cancels_without_changes(self):
        original = self.config.read_text(encoding="utf-8")
        result = self.run_cli("remove", "global:global-old", input_text="wrong\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Cancelled", result.stdout)
        self.assertEqual(self.config.read_text(encoding="utf-8"), original)
        self.assertFalse(list(self.home.glob("config.toml.backup-*")))

    def test_report_includes_app_connector_tools_and_usage(self):
        self.config.write_text(
            '[plugins."notion@local"]\nenabled = true\n', encoding="utf-8"
        )
        app_manifest = self.home / "plugins/cache/local/notion/1.0.0/.app.json"
        app_manifest.parent.mkdir(parents=True)
        app_manifest.write_text(json.dumps({"apps": {
            "notion": {"id": "app-notion", "required": True}
        }}), encoding="utf-8")
        directory = self.home / "cache/codex_app_directory/cache.json"
        directory.parent.mkdir(parents=True)
        directory.write_text(json.dumps({"connectors": [
            {"id": "app-notion", "name": "Notion"}
        ]}), encoding="utf-8")
        tools = self.home / "cache/codex_apps_tools/cache.json"
        tools.parent.mkdir(parents=True)
        tools.write_text(json.dumps({"tools": [
            {"connector_id": "app-notion", "tool": {
                "name": "notion.search", "description": "Search pages",
                "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
                "inputSchema": {"type": "object"},
            }}
        ]}), encoding="utf-8")
        db = sqlite3.connect(self.home / "thread_history_1.sqlite")
        db.execute("INSERT INTO thread_items VALUES (?, ?, ?)", (
            int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000),
            "mcpToolCall", json.dumps({"server": "codex_apps", "tool": "notion.search", "status": "completed"}),
        ))
        db.commit()
        db.close()

        report = self.root / "mcp-report.html"
        result = self.run_cli("report", "--output", str(report))
        self.assertEqual(result.returncode, 0, result.stderr)
        page = report.read_text(encoding="utf-8")
        self.assertIn("Notion", page)
        self.assertIn("notion.search", page)
        self.assertIn("Plugin enabled; connector authorization/runtime unknown", page)
        self.assertIn("1 calls; 1 completed", page)
        self.assertIn("Observed calls", page)
        self.assertIn("read-only hint: true", page)
        self.assertIn("Saved tool list: 1 tools", page)
        self.assertIn("What the saved tool descriptions mention", page)
        self.assertIn("Descriptions mention reading files or private information", page)
        self.assertIn("These are words in a saved tool list", page)
        self.assertIn('id="connection-search"', page)
        self.assertIn('<strong>To disconnect the outside account</strong>', page)
        self.assertIn('href="https://chatgpt.com/plugins" target="_blank" rel="noopener noreferrer"', page)
        self.assertIn('Find Notion under Installed.', page)
        self.assertIn('Uninstalling a plugin alone may leave an account connection in place', page)
        self.assertIn('id="activity-filter"', page)
        self.assertIn("This app is listed by a Codex plugin", page)
        self.assertIn("Last 30 days", page)
        self.assertIn("1 matching request recorded", page)
        self.assertIn("Actions in saved history", page)
        self.assertNotIn("OWASP MCP6", page)

    def test_app_name_is_escaped_in_account_connection_guidance(self):
        self.config.write_text('[plugins."notes@local"]\nenabled = true\n'
                               '[apps.notes]\nenabled = true\n', encoding="utf-8")
        manifest = self.home / "plugins/cache/local/notes/1.0.0/.app.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"apps": {"notes": {"id": "app-notes"}}}), encoding="utf-8")
        directory = self.home / "cache/codex_app_directory/cache.json"
        directory.parent.mkdir(parents=True)
        directory.write_text(json.dumps({"connectors": [
            {"id": "app-notes", "name": '<img src=x onerror="alert(1)">'}
        ]}), encoding="utf-8")
        report = self.root / "escaped-app.html"
        result = self.run_cli("report", "--output", str(report))
        self.assertEqual(result.returncode, 0, result.stderr)
        page = report.read_text(encoding="utf-8")
        self.assertNotIn('<img src=x onerror="alert(1)">', page)
        self.assertIn('&lt;img src=x onerror=&quot;alert(1)&quot;&gt;', page)
        self.assertEqual(page.count('href="https://chatgpt.com/plugins"'), 1)

    def test_missing_history_is_not_reported_as_zero_activity(self):
        (self.home / "thread_history_1.sqlite").unlink()
        report = self.root / "missing-history.html"
        result = self.run_cli("report", "--output", str(report))
        self.assertEqual(result.returncode, 0, result.stderr)
        page = report.read_text(encoding="utf-8")
        self.assertIn("Activity could not be checked: no saved Codex history files were found", page)
        self.assertIn("Activity check incomplete", page)
        self.assertNotIn("0 matching requests in the saved Codex history checked", page)

    def test_future_dated_call_is_not_recent_activity(self):
        self.config.write_text(self.config.read_text(encoding="utf-8") +
                               '\n[mcp_servers.future]\ncommand = "python"\n', encoding="utf-8")
        future = int((dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=10)).timestamp() * 1000)
        db = sqlite3.connect(self.home / "thread_history_1.sqlite")
        db.execute("INSERT INTO thread_items VALUES (?, ?, ?)",
                   (future, "mcpToolCall", json.dumps({"server": "future", "tool": "read", "status": "completed"})))
        db.commit()
        db.close()
        usage = mcp_lens.history_usage(self.home)
        self.assertEqual(usage["future"]["count"], 1)
        self.assertEqual(usage["future"]["recent_count"], 0)
        self.assertIsNone(usage["future"]["last"])

    def test_same_display_name_does_not_hide_distinct_mcp_and_app_activity(self):
        self.config.write_text(self.config.read_text(encoding="utf-8") +
                               '\n[plugins."example@local"]\nenabled = true\n', encoding="utf-8")
        manifest = self.home / "plugins/cache/local/example/1.0.0/.app.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"apps": {"notion": {"id": "app-notion"}}}), encoding="utf-8")
        directory = self.home / "cache/codex_app_directory/cache.json"
        directory.parent.mkdir(parents=True)
        directory.write_text(json.dumps({"connectors": [{"id": "app-notion", "name": "global-old"}]}), encoding="utf-8")
        db = sqlite3.connect(self.home / "thread_history_1.sqlite")
        db.execute("INSERT INTO thread_items VALUES (?, ?, ?)",
                   (int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000),
                    "mcpToolCall", json.dumps({"server": "codex_apps", "tool": "notion.search"})))
        db.commit()
        db.close()
        _, rows = mcp_lens.load_inventory(self.config, self.home)
        self.assertEqual(next(r for r in rows if r["id"] == "global:global-old")["use_count"], 2)
        self.assertEqual(next(r for r in rows if r["id"] == "connector:example@local:notion")["use_count"], 1)

    def test_two_plugins_referencing_one_connector_show_one_app(self):
        self.config.write_text(
            '[plugins."github@local"]\nenabled = true\n'
            '[plugins."security@local"]\nenabled = true\n', encoding="utf-8")
        for plugin in ("github", "security"):
            manifest = self.home / f"plugins/cache/local/{plugin}/1.0.0/.app.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"apps": {"github": {"id": "app-github"}}}), encoding="utf-8")
        directory = self.home / "cache/codex_app_directory/cache.json"
        directory.parent.mkdir(parents=True)
        directory.write_text(json.dumps({"connectors": [{"id": "app-github", "name": "GitHub"}]}), encoding="utf-8")
        db = sqlite3.connect(self.home / "thread_history_1.sqlite")
        db.execute("INSERT INTO thread_items VALUES (?, ?, ?)", (
            int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000), "mcpToolCall",
            json.dumps({"server": "codex_apps", "tool": "github.search", "status": "completed"})))
        db.commit()
        db.close()
        _, rows = mcp_lens.load_inventory(self.config, self.home)
        apps = [row for row in rows if row.get("kind") == "connector"]
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["id"], "connector:app-github")
        self.assertEqual(apps[0]["use_count"], 1)
        self.assertNotIn("history_note", apps[0])
        self.assertEqual(len(apps[0]["app_sources"]), 2)
        self.assertEqual(len(apps[0]["manifest_entries"]), 2)
        report = self.root / "single-github.html"
        self.assertEqual(self.run_cli("report", "--output", str(report)).returncode, 0)
        page = report.read_text(encoding="utf-8")
        self.assertIn("These plugin files name the same app", page)

    def test_same_connector_with_two_tool_prefixes_combines_activity(self):
        self.config.write_text(
            '[plugins."primary@local"]\nenabled = true\n'
            '[plugins."alias@local"]\nenabled = true\n', encoding="utf-8")
        for plugin, slug in (("primary", "github"), ("alias", "github_alias")):
            manifest = self.home / f"plugins/cache/local/{plugin}/1.0.0/.app.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"apps": {slug: {"id": "app-github"}}}), encoding="utf-8")
        db = sqlite3.connect(self.home / "thread_history_1.sqlite")
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        db.executemany("INSERT INTO thread_items VALUES (?, ?, ?)", [
            (stamp, "mcpToolCall", json.dumps({"server": "codex_apps", "tool": "github.read", "status": "completed"})),
            (stamp, "mcpToolCall", json.dumps({"server": "codex_apps", "tool": "github_alias.write", "status": "completed"})),
        ])
        db.commit()
        db.close()
        _, rows = mcp_lens.load_inventory(self.config, self.home)
        apps = [row for row in rows if row.get("kind") == "connector"]
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["use_count"], 2)
        self.assertEqual(len(apps[0]["tool_activity"]), 2)

    def test_app_id_keeps_same_tool_prefix_apps_separate(self):
        self.config.write_text(
            '[plugins."first@local"]\nenabled = true\n'
            '[plugins."second@local"]\nenabled = true\n', encoding="utf-8")
        for plugin, connector_id in (("first", "app-first"), ("second", "app-second")):
            manifest = self.home / f"plugins/cache/local/{plugin}/1.0.0/.app.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"apps": {"shared": {"id": connector_id}}}), encoding="utf-8")
        db = sqlite3.connect(self.home / "thread_history_1.sqlite")
        stamp = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        for connector_id in ("app-first", "app-second"):
            db.execute("INSERT INTO thread_items VALUES (?, ?, ?)", (
                stamp, "mcpToolCall", json.dumps({"server": "codex_apps", "tool": "shared.read",
                                                    "appContext": {"connectorId": connector_id}})))
        db.execute("INSERT INTO thread_items VALUES (?, ?, ?)", (
            stamp, "mcpToolCall", json.dumps({"server": "codex_apps", "tool": "shared.read"})))
        db.commit()
        db.close()
        _, rows = mcp_lens.load_inventory(self.config, self.home)
        apps = [row for row in rows if row.get("kind") == "connector"]
        self.assertEqual(len(apps), 2)
        self.assertEqual({row["connector_id"]: row["use_count"] for row in apps},
                         {"app-first": 1, "app-second": 1})
        self.assertTrue(all(row["unattributed_prefix_calls"] == 1 for row in apps))

    def test_plugin_reference_with_unchecked_history_uses_incomplete_filter(self):
        (self.home / "thread_history_1.sqlite").unlink()
        self.config.write_text('[plugins."notion@local"]\nenabled = true\n', encoding="utf-8")
        app_manifest = self.home / "plugins/cache/local/notion/1.0.0/.app.json"
        app_manifest.parent.mkdir(parents=True)
        app_manifest.write_text(json.dumps({"apps": {"notion": {"id": "app-notion"}}}),
                                encoding="utf-8")
        report = self.root / "no-history.html"
        result = self.run_cli("report", "--output", str(report))
        self.assertEqual(result.returncode, 0, result.stderr)
        page = report.read_text(encoding="utf-8")
        self.assertIn('data-activity="unavailable" data-search="notion"', page)
        self.assertIn("Activity could not be checked", page)

    def test_effective_config_can_include_project_server_without_changing_user_config(self):
        effective = {"mcp_servers": {"project-tool": {"command": "python", "enabled": True}}, "plugins": {}}
        _, rows = mcp_lens.load_inventory(self.config, self.home, config_override=effective)
        self.assertIn("project-tool", {r["name"] for r in rows})
        self.assertNotIn("global-old", {r["name"] for r in rows})
        self.assertIn("global-old", tomllib.loads(self.config.read_text(encoding="utf-8"))["mcp_servers"])

    def test_report_checks_codex_app_enabled_state_without_claiming_account_access(self):
        self.config.write_text('[plugins."notion@local"]\nenabled = true\n', encoding="utf-8")
        app_manifest = self.home / "plugins/cache/local/notion/1.0.0/.app.json"
        app_manifest.parent.mkdir(parents=True)
        app_manifest.write_text(json.dumps({"apps": {
            "notion": {"id": "app-notion"},
            "github": {"id": "app-github"},
            "canva": {"id": "app-canva"},
            "missing": {"id": "app-missing"},
        }}), encoding="utf-8")
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake_codex = fake_bin / "codex"
        fake_codex.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "for line in sys.stdin:\n"
            " request = json.loads(line)\n"
            " if request.get('id') == 1:\n"
            "  print(json.dumps({'id': 1, 'result': {}}), flush=True)\n"
            " elif request.get('id') == 2:\n"
            "  print(json.dumps({'id': 2, 'result': {'apps': ["
            "{'id': 'app-notion', 'enabled': False, 'callable': False},"
            "{'id': 'app-github', 'enabled': True, 'callable': True},"
            "{'id': 'app-canva', 'enabled': True, 'callable': False}]}}), flush=True)\n"
            " elif request.get('id') == 3:\n"
            "  print(json.dumps({'id': 3, 'result': {'config': {"
            "'plugins': {'notion@local': {'enabled': True}},"
            "'apps': {'_default': {'default_tools_approval_mode': 'prompt', 'destructive_enabled': False}}},"
            "'layers': [{'name': {'type': 'user'}}]}}), flush=True)\n"
            " elif request.get('id') == 4:\n"
            "  print(json.dumps({'id': 4, 'result': {'apps': ["
            "{'id': 'app-github', 'toolSummaries': [{'name': 'read', 'title': 'Read repository', 'isEnabled': True},"
            "{'name': 'unknown', 'title': 'Unknown action'}]}"
            "]}}), flush=True)\n"
            " else:\n"
            "  print(json.dumps({'id': request.get('id'), 'result': {}}), flush=True)\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)
        report = self.root / "live-status.html"
        result = self.run_cli("report", "--output", str(report), live=True,
                              extra_env={"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")})
        self.assertEqual(result.returncode, 0, result.stderr)
        page = report.read_text(encoding="utf-8")
        self.assertIn("Off in Codex&#x27;s general app list", page)
        self.assertIn("On with actions listed in Codex&#x27;s general app list", page)
        self.assertIn("On in Codex&#x27;s general app list; no actions listed there", page)
        self.assertIn("Apps only named by plugin files", page)
        self.assertIn("Not in Codex&#x27;s current app list", page)
        self.assertIn("Only mentioned by a plugin", page)
        self.assertIn('<strong>missing</strong><small>Only mentioned by a plugin</small>', page)
        self.assertNotIn('data-entry-id="connector:notion@local:missing"', page)
        self.assertIn("Checked Codex&#x27;s general app list", page)
        self.assertIn("Used Codex&#x27;s effective settings", page)
        self.assertIn("This does not show account permissions", page)
        self.assertIn("Codex settings say to ask before app actions by default", page)
        self.assertIn("1 enabled of 2 listed", page)
        self.assertIn("Codex did not give an on/off state for 1 action", page)
        self.assertIn("Which outside account is signed in", page)
        self.assertIn("Actions labelled destructive: off in the general Codex setting", page)
        self.assertNotIn("Connected app", page)

    def test_snapshot_reports_entry_changes_and_stores_fingerprints(self):
        snapshot = self.root / "mcp-lens-snapshot.json"
        report = self.root / "mcp-report.html"
        first = self.run_cli("report", "--output", str(report), "--snapshot", str(snapshot))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("Change tracking started", report.read_text(encoding="utf-8"))
        saved = json.loads(snapshot.read_text(encoding="utf-8"))
        self.assertEqual(saved["schema"], 2)
        self.assertTrue(all(len(value) == 64 for value in saved["entries"].values()))
        self.assertNotIn("example.invalid", snapshot.read_text(encoding="utf-8"))

        self.config.write_text(self.config.read_text(encoding="utf-8") +
                               '\n[mcp_servers.added]\ncommand = "python"\n', encoding="utf-8")
        second = self.run_cli("report", "--output", str(report), "--snapshot", str(snapshot))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Added: added", report.read_text(encoding="utf-8"))

        changed = self.config.read_text(encoding="utf-8").replace(
            '[mcp_servers.global-old]\ncommand = "python"',
            '[mcp_servers.global-old]\ncommand = "node"',
        )
        changed = changed.replace(
            '[mcp_servers.global-unknown]\nurl = "https://example.invalid/mcp"\n', ""
        )
        self.config.write_text(changed, encoding="utf-8")
        third = self.run_cli("report", "--output", str(report), "--snapshot", str(snapshot))
        self.assertEqual(third.returncode, 0, third.stderr)
        page = report.read_text(encoding="utf-8")
        self.assertIn("Changed: global-old", page)
        self.assertIn("Removed: global unknown", page)

    def test_unreadable_change_tracking_file_does_not_claim_no_changes(self):
        snapshot = self.root / "mcp-lens-snapshot.json"
        snapshot.write_text("{broken", encoding="utf-8")
        report = self.root / "mcp-report.html"
        result = self.run_cli("report", "--output", str(report), "--snapshot", str(snapshot))
        self.assertEqual(result.returncode, 0, result.stderr)
        page = report.read_text(encoding="utf-8")
        self.assertIn("The saved change-tracking file could not be read", page)
        self.assertNotIn("No changes found since the saved copy.", page)
        self.assertEqual(snapshot.read_text(encoding="utf-8"), "{broken")

    def test_snapshot_reports_specific_saved_tool_changes(self):
        self.config.write_text('[plugins."notion@local"]\nenabled = true\n', encoding="utf-8")
        app_manifest = self.home / "plugins/cache/local/notion/1.0.0/.app.json"
        app_manifest.parent.mkdir(parents=True)
        app_manifest.write_text(json.dumps({"apps": {"notion": {"id": "app-notion"}}}), encoding="utf-8")
        directory = self.home / "cache/codex_app_directory/cache.json"
        directory.parent.mkdir(parents=True)
        directory.write_text(json.dumps({"connectors": [{"id": "app-notion", "name": "Notion"}]}), encoding="utf-8")
        tools_path = self.home / "cache/codex_apps_tools/cache.json"
        tools_path.parent.mkdir(parents=True)
        tools_path.write_text(json.dumps({"tools": [{"connector_id": "app-notion", "tool": {
            "name": "notion.search", "description": "Search pages", "inputSchema": {"type": "object"}
        }}]}), encoding="utf-8")
        snapshot = self.root / "mcp-lens-snapshot.json"
        report = self.root / "mcp-report.html"
        first = self.run_cli("report", "--output", str(report), "--snapshot", str(snapshot))
        self.assertEqual(first.returncode, 0, first.stderr)
        tools_path.write_text(json.dumps({"tools": [{"connector_id": "app-notion", "tool": {
            "name": "notion.search", "description": "Search and update pages", "inputSchema": {"type": "object"}
        }}]}), encoding="utf-8")
        second = self.run_cli("report", "--output", str(report), "--snapshot", str(snapshot))
        self.assertEqual(second.returncode, 0, second.stderr)
        page = report.read_text(encoding="utf-8")
        self.assertIn("Saved tool details changed for Notion: notion.search", page)
        saved = json.loads(snapshot.read_text(encoding="utf-8"))
        self.assertNotIn("Search and update pages", json.dumps(saved))

    def test_terminal_summary_uses_plain_labels_and_sanitizes_names(self):
        class FakeTerminal(io.StringIO):
            def isatty(self):
                return True

        row = {"kind": "connector", "name": "Notes\x1b[2J\nPrivate", "plugin_key": None,
               "use_count": 2, "last_used": "2026-10-24 12:30 UTC",
               "history_scan_state": "checked",
               "runtime_app": {"enabled": True, "callable": True}}
        output = FakeTerminal()
        with contextlib.redirect_stdout(output), patch.dict(os.environ, {"NO_COLOR": "1"}):
            mcp_lens.print_terminal_summary([row], self.root, self.root / "report.html",
                                            mcp_lens.APP_STATUS_CHECKED_NOTE)
        visible = output.getvalue()
        self.assertIn("Apps with saved activity (1)", visible)
        self.assertIn("Status: On in Codex's app list; actions shown", visible)
        self.assertIn("Saved activity: Codex tried to use this 2 times", visible)
        self.assertIn("Notes [2J Private", visible)
        self.assertNotIn("\x1b", visible)

    def test_terminal_activity_marks_partial_history_even_when_calls_exist(self):
        activity = mcp_lens.terminal_activity({"use_count": 3, "last_used": None,
                                              "history_scan_state": "partial"})
        self.assertIn("3 times", activity)
        self.assertIn("activity check incomplete", activity)

    def test_terminal_banner_and_pet_can_be_still_or_animated(self):
        class FakeTerminal(io.StringIO):
            def isatty(self):
                return True

        static = FakeTerminal()
        with contextlib.redirect_stdout(static), patch.dict(os.environ, {"TERM": "xterm"}):
            with mcp_lens.TerminalCompanion(animate=False):
                pass
        self.assertIn("MCP Lens | What Codex lists on this device", static.getvalue())
        self.assertIn("Finding apps, MCP servers and saved activity", static.getvalue())
        self.assertNotIn("\x1b[2K", static.getvalue())

        animated = FakeTerminal()
        with contextlib.redirect_stdout(animated), patch.dict(os.environ, {"TERM": "xterm"}):
            with mcp_lens.TerminalCompanion(animate=True):
                time.sleep(0.35)
        self.assertIn("\r\x1b[2K", animated.getvalue())
        self.assertIn("Report ready.", animated.getvalue())

    def test_terminal_has_ascii_fallback_for_banner_and_unicode_names(self):
        class AsciiTerminal(io.StringIO):
            @property
            def encoding(self):
                return "ascii"

            def isatty(self):
                return True

        output = AsciiTerminal()
        row = {"kind": "mcp", "name": "Caf\u00e9 \u202e\x1b[2J", "plugin_key": None,
               "status": "Enabled in config (runtime unverified)",
               "use_count": 0, "history_scan_state": "checked"}
        with contextlib.redirect_stdout(output), patch.dict(os.environ, {"NO_COLOR": "1"}):
            with mcp_lens.TerminalCompanion(animate=False):
                pass
            mcp_lens.print_terminal_summary([row], self.root, self.root / "report.html",
                                            mcp_lens.APP_STATUS_CHECKED_NOTE)
        visible = output.getvalue()
        self.assertIn("#", visible)
        self.assertIn("Caf? [2J", visible)
        self.assertNotIn("\u202e", visible)
        self.assertNotIn("\x1b", visible)
        self.assertTrue(visible.isascii())

    def test_redirected_report_output_does_not_print_connection_names(self):
        report = self.root / "report.html"
        result = self.run_cli("report", "--output", str(report))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Wrote local report:", result.stdout)
        self.assertNotIn("global-old", result.stdout)
        self.assertNotIn("MCP LENS", result.stdout)


if __name__ == "__main__":
    unittest.main()
