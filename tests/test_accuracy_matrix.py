"""52 independent accuracy scenarios, completing the project's 100-case QA run.

The older suite has 48 named tests. Each case below is registered as its own
unittest method so a failed scenario is reported separately.
"""

import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import mcp_lens


NOW = int(dt.datetime(2026, 10, 26, tzinfo=dt.timezone.utc).timestamp() * 1000)
SCENARIOS = []


def scenario(number, title, group, **data):
    SCENARIOS.append({"number": number, "title": title, "group": group, **data})


# Timestamp validity determines whether a request can be called "recent".
for number, title, value, expected in (
    (49, "Ordinary millisecond timestamp", NOW, NOW),
    (50, "Numeric string timestamp", str(NOW), NOW),
    (51, "UTC ISO timestamp", "2026-10-26T00:00:00Z", NOW),
    (52, "ISO timestamp with offset", "2026-10-26T01:00:00+01:00", NOW),
    (53, "Small clock skew is accepted", NOW + 4 * 60_000, NOW + 4 * 60_000),
    (54, "Future timestamp beyond skew is rejected", NOW + 6 * 60_000, None),
    (55, "Zero timestamp is rejected", 0, None),
    (56, "Negative timestamp is rejected", -1, None),
    (57, "Boolean is not a timestamp", True, None),
    (58, "Timezone-free ISO timestamp is rejected", "2026-10-26T00:00:00", None),
):
    scenario(number, title, "time", value=value, expected=expected)


# Exact TOML targeting matters: disabling/removing one entry must not touch a neighbour.
for number, title, mode, text, target, expected in (
    (59, "Plain MCP TOML table", "path", "[mcp_servers.demo]", (), ("mcp_servers", "demo")),
    (60, "Quoted dotted MCP name", "path", '[mcp_servers."a.b"]', (), ("mcp_servers", "a.b")),
    (61, "Scoped plugin and dotted server", "path", '[plugins."@team/demo@local".mcp_servers."x.y"]', (),
     ("plugins", "@team/demo@local", "mcp_servers", "x.y")),
    (62, "Malformed table is not accepted", "path", "[mcp_servers.demo", (), None),
    (63, "Removal includes nested settings", "bounds", '[mcp_servers.demo]\ncommand="x"\n[mcp_servers.demo.env]\nA="B"\n[mcp_servers.other]\ncommand="y"\n',
     ("mcp_servers", "demo"), "[mcp_servers.demo.env]"),
    (64, "Removal stops before similarly named sibling", "bounds", '[mcp_servers.demo]\ncommand="x"\n[mcp_servers.demo2]\ncommand="y"\n',
     ("mcp_servers", "demo"), "[mcp_servers.demo2]"),
    (65, "Existing enabled flag is replaced", "disable", '[mcp_servers.demo]\nenabled=true\n[mcp_servers.other]\nenabled=true\n',
     ("mcp_servers", "demo"), False),
    (66, "Missing enabled flag is added", "disable", '[mcp_servers.demo]\ncommand="x"\n',
     ("mcp_servers", "demo"), False),
    (67, "Inline settings table is not edited blindly", "reject", 'mcp_servers = { demo = { command = "x" } }\n',
     ("mcp_servers", "demo"), None),
    (68, "Missing plugin override is appended", "plugin", '[plugins."demo@local"]\nenabled=true\n',
     ("demo@local", "tool"), False),
):
    scenario(number, title, "toml", mode=mode, text=text, target=target, expected=expected)


for number, title, mode in (
    (69, "User MCP source points to its setting", "local_mcp"),
    (70, "User app source points to its setting", "local_app"),
    (71, "Plugin MCP shows policy and package file", "plugin_mcp"),
    (72, "Project layer points to project config", "project"),
    (73, "System layer points to system file", "system"),
    (74, "Ignored layer is labelled ignored", "ignored"),
    (75, "Missing layer path is not invented", "unknown"),
    (76, "Shared app keeps both manifest sources", "merged_app"),
):
    scenario(number, title, "source", mode=mode)


for number, title, overrides, expected in (
    (77, "Enabled callable app is available", {"runtime_app": {"enabled": True, "callable": True}},
     "On with actions listed in Codex's general app list"),
    (78, "Enabled app without callable tools is limited", {"runtime_app": {"enabled": True, "callable": False}},
     "On in Codex's general app list; no actions listed there"),
    (79, "Disabled app is shown off", {"runtime_app": {"enabled": False, "callable": False}},
     "Off in Codex's general app list"),
    (80, "Folder-level off overrides general on", {"selected_folder_app_enabled": False,
     "runtime_app": {"enabled": True, "callable": True}}, "Off in Codex settings for this folder"),
    (81, "Folder-level on does not prove availability", {"selected_folder_app_enabled": True},
     "On in this folder's settings; Codex did not list this app"),
    (82, "Absent app list entry stays uncertain", {"runtime_checked": True},
     "Codex did not list this app in its general app list"),
    (83, "Enabled plugin does not prove account link", {"status": "Plugin enabled; connector authorization/runtime unknown"},
     "Plugin setting is on; app account not checked"),
    (84, "Past calls remain after setting is off", {"kind": "mcp", "status": "Disabled in config", "use_count": 2},
     "Marked off in settings; past requests remain in history"),
):
    scenario(number, title, "status", overrides=overrides, expected=expected)


for number, title, mode in (
    (85, "Uninstalled optional app is a reference", "reference"),
    (86, "Available app remains a main app", "available"),
    (87, "Installed but disabled app remains visible", "runtime_off"),
    (88, "Explicit app setting remains visible", "configured"),
    (89, "Recorded app request remains visible", "used"),
    (90, "Offline report does not claim installation", "offline"),
    (91, "Two plugins naming one absent app make one reference", "merged"),
    (92, "Config-only app remains visible", "config_only"),
):
    scenario(number, title, "report", mode=mode)


for number, title, mode in (
    (93, "Share export omits a referenced app name and path", "share"),
    (94, "Private report has owner-only permissions", "private_mode"),
    (95, "Plugin file path is escaped in HTML", "escape_path"),
    (96, "Direct removal preserves sibling server", "remove_sibling"),
    (97, "Plugin disable preserves package and creates no backup", "plugin_disable"),
    (98, "App disable uses existing app ID setting", "app_id"),
    (99, "Built-in Codex feature cannot be disabled", "builtin_refusal"),
    (100, "App without settings key cannot be disabled", "missing_key_refusal"),
):
    scenario(number, title, "safety", mode=mode)


class AccuracyMatrixTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / ".codex"
        self.home.mkdir()
        self.config = self.home / "config.toml"
        self.config.write_text("", encoding="utf-8")
        self.project = self.root / "project"
        self.project.mkdir()

    def check_time(self, case):
        self.assertEqual(mcp_lens.valid_history_ms(case["value"], NOW), case["expected"])

    def check_toml(self, case):
        mode, text, target = case["mode"], case["text"], case["target"]
        if mode == "path":
            self.assertEqual(mcp_lens.table_path(text), case["expected"])
        elif mode == "bounds":
            begin, end = mcp_lens.section_bounds_path(text, target)
            selected = text[begin:end]
            if case["number"] == 63:
                self.assertIn(case["expected"], selected)
                self.assertNotIn("[mcp_servers.other]", selected)
            else:
                self.assertNotIn(case["expected"], selected)
        elif mode == "disable":
            updated = mcp_lens.set_section_disabled(text, target)
            parsed = tomllib.loads(updated)
            self.assertIs(parsed["mcp_servers"]["demo"]["enabled"], False)
            if case["number"] == 65:
                self.assertIs(parsed["mcp_servers"]["other"]["enabled"], True)
        elif mode == "reject":
            with self.assertRaises(ValueError):
                mcp_lens.set_section_disabled(text, target)
        else:
            updated = mcp_lens.set_plugin_server_disabled(text, *target)
            parsed = tomllib.loads(updated)
            self.assertIs(parsed["plugins"]["demo@local"]["mcp_servers"]["tool"]["enabled"], False)
            self.assertIs(parsed["plugins"]["demo@local"]["enabled"], True)

    def check_source(self, case):
        mode = case["mode"]
        self.config.write_text("", encoding="utf-8")
        layers = []
        row = {"kind": "mcp", "plugin_key": None, "name": "demo"}
        if mode == "local_mcp":
            self.config.write_text('[mcp_servers.demo]\ncommand="x"\n')
        elif mode == "local_app":
            self.config.write_text('[apps.notes]\nenabled=true\n')
            row = {"kind": "connector", "plugin_key": None, "name": "Notes", "app_slug": "notes"}
        elif mode == "plugin_mcp":
            self.config.write_text('[plugins."demo@local"]\nenabled=true\n')
            row.update(plugin_key="demo@local", manifest_path=str(self.root / 'cache/.mcp.json'))
        elif mode in {"project", "system", "ignored", "unknown"}:
            name = ({"type": "project", "dotCodexFolder": str(self.project / '.codex')}
                    if mode == "project" else
                    {"type": "system", "file": str(self.root / 'managed.toml')}
                    if mode in {"system", "ignored"} else {"type": "sessionFlags"})
            layers = [{"name": name, "entries": {"mcp_servers": ["demo"]},
                       "ignored": mode == "ignored"}]
        elif mode == "merged_app":
            row = {"kind": "connector", "plugin_key": "demo@local", "name": "Notes", "app_slug": "notes",
                   "manifest_entries": [(str(self.root / 'one/.app.json'), 'notes'),
                                        (str(self.root / 'two/.app.json'), 'notes')]}
        mcp_lens.annotate_source_locations([row], self.config, layers)
        found = row["source_locations"]
        if mode == "unknown":
            self.assertEqual(found, [])
        elif mode == "plugin_mcp":
            self.assertEqual({x["entry"] for x in found}, {"plugins.demo@local", "mcpServers.demo"})
        elif mode == "merged_app":
            self.assertEqual(len(found), 2)
            self.assertEqual({x["entry"] for x in found}, {"apps.notes"})
        else:
            self.assertEqual(len(found), 1)
            self.assertTrue(found[0]["path"].endswith("config.toml") if mode in {"local_mcp", "local_app", "project"}
                            else found[0]["path"].endswith("managed.toml"))
            self.assertEqual(found[0]["label"] == "Ignored Codex settings entry", mode == "ignored")

    def check_status(self, case):
        row = {"kind": "connector", "plugin_key": "demo@local", "status": "Installed plugin; enabled/authorization state unknown",
               "use_count": 0, "recent_count": 0}
        row.update(case["overrides"])
        self.assertEqual(mcp_lens.friendly_status(row), case["expected"])

    def report_for(self, mode):
        config_text = '[plugins."bundle@local"]\nenabled=true\n'
        if mode == "configured":
            config_text += '[apps.optional]\nenabled=true\n'
        if mode == "config_only":
            config_text = '[apps.optional]\nenabled=true\n'
        self.config.write_text(config_text, encoding="utf-8")
        if mode != "config_only":
            for plugin in (("bundle", "second") if mode == "merged" else ("bundle",)):
                path = self.home / f"plugins/cache/local/{plugin}/1.0.0/.app.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"apps": {"optional": {"id": "app-optional"}}}), encoding="utf-8")
            if mode == "merged":
                self.config.write_text(config_text + '[plugins."second@local"]\nenabled=true\n', encoding="utf-8")
        if mode == "used":
            database = sqlite3.connect(self.home / "thread_history_1.sqlite")
            database.execute("CREATE TABLE thread_items (created_at_ms INTEGER, item_type TEXT, item_json TEXT)")
            database.execute("INSERT INTO thread_items VALUES (?,?,?)", (
                int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000), "mcpToolCall",
                json.dumps({"server": "codex_apps", "tool": "optional.read",
                            "appContext": {"connectorId": "app-optional"}, "status": "completed"})))
            database.commit()
            database.close()
        runtime_app = ({"enabled": True, "callable": True} if mode == "available" else
                       {"enabled": False, "callable": False} if mode == "runtime_off" else None)
        runtime = {"apps": {"app-optional": runtime_app} if runtime_app else {},
                   "app_note": mcp_lens.APP_STATUS_CHECKED_NOTE, "app_summaries": {},
                   "config": tomllib.loads(self.config.read_text()), "config_note": "Checked effective config.",
                   "layers": []}
        output = self.root / "report.html"
        argv = ["mcp_lens.py", "--home", str(self.home), "report", "--project", str(self.project),
                "--output", str(output)]
        if mode == "offline":
            argv.append("--offline")
        with patch.object(sys, "argv", argv), patch.object(mcp_lens, "codex_runtime_snapshot", return_value=runtime):
            with contextlib.redirect_stdout(io.StringIO()):
                mcp_lens.main()
        return output.read_text(encoding="utf-8")

    def check_report(self, case):
        mode = case["mode"]
        page = self.report_for(mode)
        reference = mode in {"reference", "offline", "merged"}
        self.assertEqual("Only mentioned by a plugin" in page, reference)
        self.assertEqual('id="plugin-app-references"' in page, reference)
        if reference:
            self.assertNotIn('data-entry-id="connector:', page)
            self.assertIn("That does not establish an account connection", page)
            if mode == "merged":
                self.assertEqual(page.count("Only mentioned by a plugin"), 1)
                self.assertIn("second/1.0.0/.app.json", page)
        else:
            self.assertIn("App (connector)", page)
            if mode not in {"config_only", "runtime_off"}:
                self.assertIn("data-entry-id=", page)
        if mode == "runtime_off":
            self.assertIn("Off in Codex&#x27;s general app list", page)
        if mode == "used":
            self.assertIn("Apps (connectors) with recorded requests", page)
            self.assertIn("1 recorded request", page)
        if mode == "offline":
            self.assertIn("Current app list was not checked", page)
            self.assertIn('<span class="entry-use">Activity could not be checked</span>', page)

    def check_safety(self, case):
        mode = case["mode"]
        if mode in {"share", "private_mode", "escape_path"}:
            row = {"kind": "connector", "plugin_key": "demo@local", "name": "Secret & Notes",
                   "status": "Installed plugin; enabled/authorization state unknown",
                   "id": "connector:demo@local:notes", "app_slug": "notes", "connector_id": "app-notes",
                   "history_key": "connector:notes", "scope": "Plugin demo@local",
                   "use_count": 0, "recent_count": 0, "completed_count": 0,
                   "first_seen": None, "last_used": None, "install": "Unknown",
                   "approval": "Unknown", "tool_activity": {}, "reference_only": True,
                   "runtime_checked": True, "source_locations": [{"label": "Saved plugin file",
                     "path": str(self.root / 'a&b/.app.json'), "entry": "apps.notes"}]}
            output = self.root / "private.html"
            mcp_lens.render_html([row], self.config, output)
            if mode == "private_mode":
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            if mode == "escape_path":
                page = output.read_text()
                self.assertIn("a&amp;b/.app.json", page)
                self.assertNotIn("a&b/.app.json", page)
            if mode == "share":
                share = self.root / "share.html"
                mcp_lens.render_share_html([row], share, {})
                page = share.read_text()
                self.assertIn("Apps only named by plugin files</dt><dd>1", page)
                self.assertNotIn("Secret & Notes", page)
                self.assertNotIn(str(self.root), page)
        elif mode == "remove_sibling":
            original = '[mcp_servers.demo]\ncommand="x"\n[mcp_servers.demo.env]\nA="B"\n[mcp_servers.demo2]\ncommand="y"\n'
            self.config.write_text(original)
            result = mcp_lens.remove_direct_mcp_entry(self.config, {"kind": "mcp", "plugin_key": None, "name": "demo"})
            parsed = tomllib.loads(self.config.read_text())
            self.assertIn("removed", result.lower())
            self.assertNotIn("demo", parsed["mcp_servers"])
            self.assertEqual(parsed["mcp_servers"]["demo2"]["command"], "y")
        elif mode == "plugin_disable":
            self.config.write_text('[plugins."demo@local".mcp_servers.tool]\nenabled=true\n')
            package = self.root / ".mcp.json"
            package.write_text('{"mcpServers":{"tool":{}}}')
            original_package = package.read_bytes()
            mcp_lens.disable_entry(self.config, {"kind": "mcp", "plugin_key": "demo@local", "name": "tool"})
            self.assertFalse(tomllib.loads(self.config.read_text())["plugins"]["demo@local"]["mcp_servers"]["tool"]["enabled"])
            self.assertEqual(package.read_bytes(), original_package)
            self.assertFalse(list(self.home.glob("*.backup*")))
        elif mode == "app_id":
            self.config.write_text('[apps."app-123"]\nenabled=true\n')
            mcp_lens.disable_entry(self.config, {"kind": "connector", "plugin_key": "demo@local",
                                                  "app_slug": "notes", "connector_id": "app-123"})
            parsed = tomllib.loads(self.config.read_text())
            self.assertFalse(parsed["apps"]["app-123"]["enabled"])
            self.assertNotIn("notes", parsed["apps"])
        elif mode == "builtin_refusal":
            self.config.write_text('[plugins."codex-app-tools@openai-bundled"]\nenabled=true\n')
            with self.assertRaises(ValueError):
                mcp_lens.disable_entry(self.config, {"kind": "mcp", "plugin_key": "codex-app-tools@openai-bundled",
                                                      "name": "codex_app"})
        else:
            with self.assertRaises(ValueError):
                mcp_lens.disable_entry(self.config, {"kind": "connector", "plugin_key": "demo@local",
                                                      "app_slug": "", "connector_id": "app-123"})


def make_test(case):
    def run(self):
        getattr(self, "check_" + case["group"])(case)
    run.__doc__ = case["title"]
    return run


for item in SCENARIOS:
    name = "test_%03d_%s" % (item["number"], "_".join(item["title"].lower().split())[:60])
    setattr(AccuracyMatrixTests, name, make_test(item))


assert len(SCENARIOS) == 52
assert [item["number"] for item in SCENARIOS] == list(range(49, 101))
