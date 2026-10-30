#!/usr/bin/env python3
"""MCP Lens: local inventory and safe cleanup helper for Codex MCP entries.

Reads user MCP configuration, cached plugin MCP manifests, and local thread
history. It never connects to MCP servers.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import queue
import re
import hashlib
import http.server
import shutil
import shlex
import secrets
import sqlite3
import subprocess
import tempfile
import sys
import threading
import time
import textwrap
import tomllib
import unicodedata
import webbrowser
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote, urlsplit


DEFAULT_CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
APP_STATUS_CHECKED_NOTE = ("Checked Codex's general app list, which may differ from a selected folder's settings. "
                           "This does not show account permissions.")


def codex_runtime_snapshot(codex_home: Path, project_dir: Path) -> dict:
    """Read app state and layered settings from Codex without invoking app tools."""
    snapshot = {
        "apps": {}, "app_note": "App status could not be checked.",
        "app_summaries": {},
        "config": None, "config_note": "Only the user settings file was checked; effective settings are unavailable.",
        "layers": [],
        "skills": [], "skills_state": "unavailable", "skills_errors": 0,
        "hooks": [], "hooks_state": "unavailable", "hooks_errors": 0, "hooks_warnings": 0,
    }
    executable = shutil.which("codex")
    if not executable:
        snapshot["app_note"] = "Codex command not found; app status could not be checked."
        return snapshot
    try:
        process = subprocess.Popen(
            [executable, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            env={**os.environ, "CODEX_HOME": str(codex_home)},
        )
    except OSError:
        snapshot["app_note"] = "Codex could not be started to check app status."
        return snapshot

    messages: queue.Queue[dict | None] = queue.Queue()

    def read_responses() -> None:
        try:
            assert process.stdout is not None
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    messages.put(message)
        finally:
            messages.put(None)

    reader_thread = threading.Thread(target=read_responses, daemon=True)
    reader_thread.start()

    def send(message: dict) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def response(request_id: int, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                item = messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                return None
            if item is None:
                return None
            if item.get("id") == request_id:
                return item

    try:
        send({"method": "initialize", "id": 1, "params": {
            "clientInfo": {"name": "mcp_lens", "title": "MCP Lens", "version": "0.1.0"}}})
        initialized = response(1, 5)
        if not initialized or not isinstance(initialized.get("result"), dict):
            snapshot["app_note"] = "Codex did not provide current app status."
            return snapshot
        send({"method": "initialized", "params": {}})
        send({"method": "app/installed", "id": 2, "params": {"forceRefresh": True}})
        installed = response(2, 12)
        apps = installed.get("result", {}).get("apps") if isinstance(installed, dict) and isinstance(installed.get("result"), dict) else None
        if isinstance(apps, list):
            for item in apps:
                if (isinstance(item, dict) and isinstance(item.get("id"), str)
                        and isinstance(item.get("enabled"), bool)
                        and isinstance(item.get("callable"), bool)):
                    snapshot["apps"][item["id"]] = {"enabled": item["enabled"], "callable": item["callable"]}
            snapshot["app_note"] = APP_STATUS_CHECKED_NOTE
        elif installed:
            snapshot["app_note"] = "Codex returned an app list this version cannot read."
        else:
            snapshot["app_note"] = "Codex did not provide current app status."
        send({"method": "config/read", "id": 3,
              "params": {"cwd": str(project_dir), "includeLayers": True}})
        config_reply = response(3, 8)
        config_result = config_reply.get("result") if isinstance(config_reply, dict) else None
        config = config_result.get("config") if isinstance(config_result, dict) else None
        if isinstance(config, dict):
            snapshot["config"] = config
            layers = config_result.get("layers")
            if isinstance(layers, list):
                # Keep only entry names and layer locations. Settings values can
                # include secrets and must never make it into the HTML report.
                for layer in layers:
                    if not isinstance(layer, dict) or not isinstance(layer.get("name"), dict):
                        continue
                    layer_config = layer.get("config")
                    if not isinstance(layer_config, dict):
                        continue
                    snapshot["layers"].append({
                        "name": layer["name"],
                        "entries": {section: list(layer_config.get(section, {}))
                                    for section in ("mcp_servers", "apps", "plugins")
                                    if isinstance(layer_config.get(section), dict)},
                        "ignored": bool(layer.get("disabledReason")),
                    })
            snapshot["config_note"] = f"Used Codex's effective settings for {project_dir}."
        else:
            snapshot["config_note"] = "Codex could not provide effective settings; only the user settings file was checked."
        app_ids = list(snapshot["apps"])
        for offset in range(0, len(app_ids), 100):
            request_id = 4 + offset // 100
            send({"method": "app/read", "id": request_id,
                  "params": {"appIds": app_ids[offset:offset + 100], "includeTools": True}})
            details = response(request_id, 3)
            detailed_apps = (details.get("result", {}).get("apps") if isinstance(details, dict)
                             and isinstance(details.get("result"), dict) else None)
            if isinstance(detailed_apps, list):
                for app in detailed_apps:
                    if isinstance(app, dict) and isinstance(app.get("id"), str):
                        summaries = app.get("toolSummaries")
                        if isinstance(summaries, list):
                            snapshot["app_summaries"][app["id"]] = [
                                {"title": str(tool.get("title") or tool.get("name") or "Action"),
                                 "enabled": tool.get("isEnabled")}
                                for tool in summaries if isinstance(tool, dict)]
        next_id = 4 + (len(app_ids) + 99) // 100
        for method, request_id, item_key, state_key in (
            ("skills/list", next_id, "skills", "skills_state"),
            ("hooks/list", next_id + 1, "hooks", "hooks_state"),
        ):
            try:
                params = {"cwds": [str(project_dir)]}
                if method == "skills/list":
                    params["forceReload"] = True
                send({"method": method, "id": request_id, "params": params})
                reply = response(request_id, 8)
            except (BrokenPipeError, OSError, ValueError):
                reply = None
            result = reply.get("result") if isinstance(reply, dict) else None
            data = result.get("data") if isinstance(result, dict) else None
            if not isinstance(data, list):
                continue
            selected_cwd = os.path.normcase(os.path.realpath(project_dir))
            # An invalid cwd in one entry must not discard an earlier, valid
            # app-status result or hide the other discovery response.
            def matches_selected_folder(item: object) -> bool:
                if not isinstance(item, dict) or not isinstance(item.get("cwd"), str):
                    return False
                try:
                    return os.path.normcase(os.path.realpath(item["cwd"])) == selected_cwd
                except (OSError, ValueError):
                    return False

            entry = next((item for item in data if matches_selected_folder(item)), None)
            if not isinstance(entry, dict) or not isinstance(entry.get(item_key), list):
                continue
            visible_fields = (("name", "description", "enabled", "scope", "path", "pluginId")
                              if item_key == "skills" else
                              ("eventName", "handlerType", "enabled", "source", "sourcePath",
                               "trustStatus"))
            snapshot[item_key] = [
                {field: item[field] for field in visible_fields if field in item}
                for item in entry[item_key] if isinstance(item, dict)
            ]
            errors = entry.get("errors")
            error_count = len(errors) if isinstance(errors, list) else 0
            snapshot[f"{item_key}_errors"] = error_count
            if item_key == "hooks":
                warnings = entry.get("warnings")
                snapshot["hooks_warnings"] = len(warnings) if isinstance(warnings, list) else 0
            snapshot[state_key] = "partial" if error_count else "checked"
        return snapshot
    except (BrokenPipeError, OSError, ValueError):
        if snapshot["app_note"] == "App status could not be checked.":
            snapshot["app_note"] = "Codex could not provide current app status."
        return snapshot
    finally:
        try:
            if process.poll() is None:
                process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            process.wait()
        reader_thread.join(timeout=1)
        for pipe in (process.stdin, process.stdout):
            if pipe is not None:
                pipe.close()


def utc_date(value: float | int | None, *, milliseconds: bool = False) -> str | None:
    if value is None:
        return None
    try:
        stamp = float(value) / (1000 if milliseconds else 1)
        return dt.datetime.fromtimestamp(stamp, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OSError, OverflowError):
        return None


def normalize_saved_folder(value: object) -> str | None:
    """Normalize a saved starting folder without trusting a damaged record."""
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    try:
        if os.path.isabs(value):
            return os.path.normcase(os.path.realpath(value))
    except (OSError, ValueError):
        pass
    return None


def thread_folders(codex_home: Path, coverage: dict) -> dict[str, str]:
    """Read known starting folders without opening conversation contents."""
    folders: dict[str, str] = {}
    conflicts: set[str] = set()
    for state_path in sorted(codex_home.glob("state_*.sqlite")):
        try:
            db = sqlite3.connect(f"file:{quote(str(state_path))}?mode=ro", uri=True, timeout=1)
            try:
                columns = {r[1] for r in db.execute("PRAGMA table_info(threads)")}
                if not {"id", "cwd"}.issubset(columns):
                    coverage["folder_files_unsupported"] += 1
                    continue
                for thread_id, cwd in db.execute("SELECT id, cwd FROM threads"):
                    normalized = normalize_saved_folder(cwd)
                    if not isinstance(thread_id, str) or normalized is None:
                        continue
                    if thread_id in folders and folders[thread_id] != normalized:
                        conflicts.add(thread_id)
                    else:
                        folders[thread_id] = normalized
                coverage["folder_files_checked"] += 1
            finally:
                db.close()
        except (sqlite3.Error, OSError):
            coverage["folder_files_unreadable"] += 1
    for pattern in ("sessions/**/*.jsonl", "archived_sessions/**/*.jsonl"):
        for session_path in sorted(codex_home.glob(pattern)):
            try:
                with session_path.open("r", encoding="utf-8") as session_file:
                    for line in session_file:
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(event, dict) or event.get("type") != "session_meta":
                            continue
                        metadata = event.get("payload")
                        if not isinstance(metadata, dict):
                            break
                        thread_id = metadata.get("id") or metadata.get("session_id")
                        cwd = metadata.get("cwd")
                        normalized = normalize_saved_folder(cwd)
                        if isinstance(thread_id, str) and normalized is not None:
                            if thread_id in folders and folders[thread_id] != normalized:
                                conflicts.add(thread_id)
                            else:
                                folders[thread_id] = normalized
                        break
            except (OSError, UnicodeError):
                # The full history scan reports unreadable session files.
                continue
    for thread_id in conflicts:
        folders.pop(thread_id, None)
    coverage["folder_conflicts"] = len(conflicts)
    return folders


def valid_history_ms(value: object, now_ms: int) -> int | None:
    """Accept only plausible timestamps from either supported local format."""
    try:
        if isinstance(value, str) and not value.strip().lstrip("-+").isdigit():
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            stamp = int(parsed.timestamp() * 1000)
        elif isinstance(value, bool):
            return None
        else:
            stamp = int(value)
        return stamp if 0 < stamp <= now_ms + 5 * 60 * 1000 else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def folder_file_changes(codex_home: Path, project_dir: Path) -> dict:
    """List completed Codex fileChange items whose recorded paths are in a folder.

    This is a view of saved edit items, not a filesystem audit. Command and
    external-tool writes are intentionally not inferred from their text.
    """
    selected = os.path.normcase(os.path.realpath(project_dir))
    now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    folders = thread_folders(codex_home, defaultdict(int))
    seen_ids: set[str] = set()
    events: list[tuple[int | None, str, str]] = []
    coverage = {"database_checked": 0, "database_unreadable": 0,
                "database_unsupported": 0, "session_checked": 0,
                "session_unreadable": 0, "records_unreadable": 0,
                "paths_unresolved": 0}

    def record(item: object, timestamp: object, thread_id: object) -> None:
        if not isinstance(item, dict) or item.get("status") != "completed":
            return
        item_id = item.get("id")
        if isinstance(item_id, str) and item_id in seen_ids:
            return
        changes = item.get("changes")
        if not isinstance(changes, list):
            coverage["records_unreadable"] += 1
            return
        stamp = valid_history_ms(timestamp, now_ms)
        resolved_change = False
        for change in changes:
            if not isinstance(change, dict) or not isinstance(change.get("path"), str):
                coverage["records_unreadable"] += 1
                continue
            raw_path = change["path"]
            if not raw_path or "\x00" in raw_path:
                coverage["records_unreadable"] += 1
                continue
            if os.path.isabs(raw_path):
                resolved = os.path.normcase(os.path.realpath(raw_path))
            else:
                cwd = folders.get(thread_id) if isinstance(thread_id, str) else None
                if cwd is None:
                    coverage["paths_unresolved"] += 1
                    continue
                resolved = os.path.normcase(os.path.realpath(os.path.join(cwd, raw_path)))
            resolved_change = True
            try:
                if os.path.commonpath((selected, resolved)) != selected:
                    continue
                relative = os.path.relpath(resolved, selected)
            except ValueError:
                continue
            kind = change.get("kind")
            kind_name = kind.get("type") if isinstance(kind, dict) else kind
            action = {"add": "Added", "update": "Edited", "delete": "Deleted"}.get(kind_name, "Changed")
            events.append((stamp, relative, action))
        # A broken copy of an item must not hide a usable copy in another
        # database or session log with the same ID.
        if resolved_change and isinstance(item_id, str):
            seen_ids.add(item_id)

    for db_path in sorted(codex_home.glob("thread_history*.sqlite")):
        try:
            db = sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True, timeout=1)
            try:
                columns = {row[1] for row in db.execute("PRAGMA table_info(thread_items)")}
                if not {"created_at_ms", "item_json", "item_type"}.issubset(columns):
                    coverage["database_unsupported"] += 1
                    continue
                thread_col = "thread_id" if "thread_id" in columns else "NULL"
                for timestamp, raw, thread_id in db.execute(
                    f"SELECT created_at_ms, item_json, {thread_col} FROM thread_items WHERE item_type='fileChange'"
                ):
                    try:
                        item = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        coverage["records_unreadable"] += 1
                        continue
                    record(item, timestamp, thread_id)
                coverage["database_checked"] += 1
            finally:
                db.close()
        except (sqlite3.Error, OSError):
            coverage["database_unreadable"] += 1

    for pattern in ("sessions/**/*.jsonl", "archived_sessions/**/*.jsonl"):
        for session_path in sorted(codex_home.glob(pattern)):
            try:
                with session_path.open("r", encoding="utf-8") as session_file:
                    thread_id = None
                    for line in session_file:
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            coverage["records_unreadable"] += 1
                            continue
                        if not isinstance(event, dict):
                            continue
                        if event.get("type") == "session_meta":
                            meta = event.get("payload")
                            if isinstance(meta, dict):
                                thread_id = meta.get("id") or meta.get("session_id")
                        if event.get("type") != "event_msg":
                            continue
                        payload = event.get("payload")
                        if not isinstance(payload, dict) or payload.get("type") != "item_completed":
                            continue
                        item = payload.get("item")
                        if isinstance(item, dict) and item.get("type") in {"FileChange", "fileChange"}:
                            record(item, payload.get("completed_at_ms") or event.get("timestamp"),
                                   payload.get("thread_id") or thread_id)
                coverage["session_checked"] += 1
            except (OSError, UnicodeError):
                coverage["session_unreadable"] += 1

    events.sort(key=lambda event: event[0] if event[0] is not None else -1, reverse=True)
    return {"events": events[:30], "count": len(events),
            "files": len({event[1] for event in events}), "coverage": coverage}


def keep_recent_event(record: dict, stamp: int | None, tool: object, status: object) -> None:
    """Keep a few safe call facts; never retain arguments or results."""
    if stamp is None or not isinstance(tool, str) or not tool:
        return
    events = record["events"]
    events.append((stamp, tool, status if isinstance(status, str) else "unknown"))
    events.sort(key=lambda event: event[0], reverse=True)
    del events[5:]


def history_usage(codex_home: Path, coverage: dict | None = None,
                  project_dir: Path | None = None) -> dict[str, dict[str, object]]:
    """Aggregate exact MCP server-name matches from local Codex thread history."""
    now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    recent_cutoff_ms = now_ms - 30 * 24 * 60 * 60 * 1000
    scan = {"files_found": 0, "files_checked": 0, "files_unreadable": 0,
            "files_unsupported": 0, "records_seen": 0, "records_unreadable": 0,
            "records_undated": 0, "oldest_call_ms": None, "newest_call_ms": None,
            "session_files_checked": 0, "session_files_unreadable": 0,
            "session_records_unreadable": 0, "session_calls_missing": 0,
            "session_calls_added": 0, "records_duplicate": 0,
            "folder_files_checked": 0, "folder_files_unreadable": 0,
            "folder_files_unsupported": 0, "folder_conflicts": 0,
            "calls_in_selected_folder": 0, "calls_in_other_folders": 0,
            "calls_with_unknown_folder": 0}
    folders = thread_folders(codex_home, scan) if project_dir is not None else {}
    selected_folder = (os.path.normcase(os.path.realpath(project_dir))
                       if project_dir is not None else None)
    database_call_ids: set[str] = set()
    usage: dict[str, dict[str, object]] = defaultdict(lambda: {
        "count": 0, "recent_count": 0, "completed": 0, "first": None, "last": None, "last_status": None,
        "events": [],
        "selected_folder_count": 0, "other_folder_count": 0, "unknown_folder_count": 0,
        "tools": defaultdict(lambda: {"count": 0, "recent_count": 0, "completed": 0, "first": None, "last": None, "last_status": None, "read_only_hints": set(), "target_types": defaultdict(int), "recent_target_types": defaultdict(int), "target_names": set(), "target_hosts": set()}),
    })
    for db_path in sorted(codex_home.glob("thread_history*.sqlite")):
        scan["files_found"] += 1
        try:
            db = sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True, timeout=1)
            try:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                columns = ({r[1] for r in db.execute("PRAGMA table_info(thread_items)")}
                           if "thread_items" in tables else set())
                if not {"created_at_ms", "item_json", "item_type"}.issubset(columns):
                    scan["files_unsupported"] += 1
                    continue
                id_column = "item_id" if "item_id" in columns else "NULL"
                thread_column = "thread_id" if "thread_id" in columns else "NULL"
                rows = db.execute(
                    f"SELECT created_at_ms, item_json, {id_column}, {thread_column} FROM thread_items WHERE item_type='mcpToolCall'"
                )
                for timestamp, raw, stored_id, thread_id in rows:
                    scan["records_seen"] += 1
                    try:
                        item = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        scan["records_unreadable"] += 1
                        continue
                    if not isinstance(item, dict):
                        scan["records_unreadable"] += 1
                        continue
                    server = item.get("server")
                    if not isinstance(server, str) or not server:
                        scan["records_unreadable"] += 1
                        continue
                    call_id = item.get("id") if isinstance(item.get("id"), str) else stored_id
                    if isinstance(call_id, str) and call_id:
                        if call_id in database_call_ids:
                            scan["records_duplicate"] += 1
                            continue
                        database_call_ids.add(call_id)
                    # Prefer the recorded app ID. Tool prefixes are a fallback
                    # for older records that do not include one.
                    if server == "codex_apps":
                        context = item.get("appContext")
                        connector_id = (context.get("connectorId") if isinstance(context, dict)
                                        else item.get("connectorId"))
                        if isinstance(connector_id, str) and connector_id:
                            server = f"connector-id:{connector_id}"
                        elif isinstance(item.get("tool"), str):
                            connector = item["tool"].split(".", 1)[0]
                            if connector:
                                server = f"connector:{connector}"
                    record = usage[server]
                    record["count"] = int(record["count"]) + 1
                    if selected_folder is not None:
                        cwd = folders.get(thread_id) if isinstance(thread_id, str) else None
                        if cwd is None:
                            record["unknown_folder_count"] += 1
                            scan["calls_with_unknown_folder"] += 1
                        elif cwd == selected_folder:
                            record["selected_folder_count"] += 1
                            scan["calls_in_selected_folder"] += 1
                        else:
                            record["other_folder_count"] += 1
                            scan["calls_in_other_folders"] += 1
                    if item.get("status") == "completed":
                        record["completed"] = int(record["completed"]) + 1
                    stamp = valid_history_ms(timestamp, now_ms)
                    if stamp is None:
                        scan["records_undated"] += 1
                    if stamp is not None:
                        scan["oldest_call_ms"] = (stamp if scan["oldest_call_ms"] is None
                                                  else min(scan["oldest_call_ms"], stamp))
                        scan["newest_call_ms"] = (stamp if scan["newest_call_ms"] is None
                                                  else max(scan["newest_call_ms"], stamp))
                        record["first"] = stamp if record["first"] is None else min(int(record["first"]), stamp)
                        if record["last"] is None or stamp >= int(record["last"]):
                            record["last"] = stamp
                            record["last_status"] = item.get("status") or "unknown outcome"
                        if stamp >= recent_cutoff_ms:
                            record["recent_count"] = int(record["recent_count"]) + 1
                    tool_name = item.get("tool")
                    keep_recent_event(record, stamp, tool_name, item.get("status"))
                    if isinstance(tool_name, str) and tool_name:
                        tool_record = record["tools"][tool_name]
                        tool_record["count"] += 1
                        if item.get("status") == "completed":
                            tool_record["completed"] += 1
                        if stamp is not None:
                            tool_record["first"] = stamp if tool_record["first"] is None else min(tool_record["first"], stamp)
                            if tool_record["last"] is None or stamp >= tool_record["last"]:
                                tool_record["last"] = stamp
                                tool_record["last_status"] = item.get("status") or "unknown outcome"
                            if stamp >= recent_cutoff_ms:
                                tool_record["recent_count"] += 1
                        hint = item.get("readOnlyHint")
                        if isinstance(hint, bool):
                            tool_record["read_only_hints"].add(hint)
                        # Keep only safe, useful location hints for Codex's
                        # open-in-app tool. Never retain full paths or URLs.
                        arguments = item.get("arguments")
                        target = arguments.get("target") if isinstance(arguments, dict) else None
                        if tool_name == "open_in_codex" and isinstance(target, dict):
                            target_type = target.get("type")
                            if isinstance(target_type, str):
                                tool_record["target_types"][target_type] += 1
                                if stamp is not None and stamp >= recent_cutoff_ms:
                                    tool_record["recent_target_types"][target_type] += 1
                            if target_type == "file" and isinstance(target.get("path"), str):
                                filename = Path(target["path"]).name
                                if filename:
                                    tool_record["target_names"].add(filename)
                            elif target_type == "browser" and isinstance(target.get("url"), str):
                                try:
                                    host = urlsplit(target["url"]).hostname
                                except ValueError:
                                    host = None
                                if host:
                                    tool_record["target_hosts"].add(host.lower())
                scan["files_checked"] += 1
            finally:
                db.close()
        except (sqlite3.Error, OSError):
            scan["files_unreadable"] += 1
            continue
    # Session logs can overlap with the history database. Compare stable call
    # IDs instead of adding their records again and inflating usage counts.
    session_call_ids: set[str] = set()
    missing_session_calls: list[tuple[dict, object, object, object]] = []
    session_paths = sorted((*codex_home.glob("sessions/**/*.jsonl"),
                            *codex_home.glob("archived_sessions/**/*.jsonl")))
    for session_path in session_paths:
        try:
            with session_path.open("r", encoding="utf-8") as session_file:
                session_thread_id = None
                for line in session_file:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        scan["session_records_unreadable"] += 1
                        continue
                    if isinstance(event, dict) and event.get("type") == "session_meta":
                        metadata = event.get("payload")
                        if isinstance(metadata, dict):
                            candidate = metadata.get("id") or metadata.get("session_id")
                            if isinstance(candidate, str):
                                session_thread_id = candidate
                    if not isinstance(event, dict) or event.get("type") != "event_msg":
                        continue
                    payload = event.get("payload")
                    if not isinstance(payload, dict) or payload.get("type") != "item_completed":
                        continue
                    item = payload.get("item")
                    if isinstance(item, dict) and item.get("type") in {"McpToolCall", "mcpToolCall"}:
                        if not isinstance(item.get("server"), str) or not item["server"]:
                            scan["session_records_unreadable"] += 1
                            continue
                        call_id = item.get("id")
                        if isinstance(call_id, str) and call_id:
                            if call_id not in session_call_ids:
                                session_call_ids.add(call_id)
                                if call_id not in database_call_ids:
                                    missing_session_calls.append((item, payload.get("completed_at_ms"),
                                                                  event.get("timestamp"),
                                                                  payload.get("thread_id") or session_thread_id))
                        else:
                            scan["session_records_unreadable"] += 1
            scan["session_files_checked"] += 1
        except (OSError, UnicodeError):
            scan["session_files_unreadable"] += 1
    scan["session_calls_missing"] = len(missing_session_calls)
    for item, completed_at, event_time, thread_id in missing_session_calls:
        server = item.get("server")
        if not isinstance(server, str) or not server:
            scan["session_records_unreadable"] += 1
            continue
        if server == "codex_apps":
            context = item.get("appContext")
            connector_id = (context.get("connectorId") if isinstance(context, dict)
                            else item.get("connectorId"))
            if isinstance(connector_id, str) and connector_id:
                server = f"connector-id:{connector_id}"
            elif isinstance(item.get("tool"), str):
                connector = item["tool"].split(".", 1)[0]
                if connector:
                    server = f"connector:{connector}"
        record = usage[server]
        record["count"] += 1
        scan["session_calls_added"] += 1
        if item.get("status") == "completed":
            record["completed"] += 1
        if selected_folder is not None:
            cwd = folders.get(thread_id) if isinstance(thread_id, str) else None
            if cwd is None:
                record["unknown_folder_count"] += 1
                scan["calls_with_unknown_folder"] += 1
            elif cwd == selected_folder:
                record["selected_folder_count"] += 1
                scan["calls_in_selected_folder"] += 1
            else:
                record["other_folder_count"] += 1
                scan["calls_in_other_folders"] += 1
        stamp = valid_history_ms(completed_at, now_ms)
        if stamp is None:
            stamp = valid_history_ms(event_time, now_ms)
        if stamp is None:
            scan["records_undated"] += 1
        else:
            scan["oldest_call_ms"] = stamp if scan["oldest_call_ms"] is None else min(scan["oldest_call_ms"], stamp)
            scan["newest_call_ms"] = stamp if scan["newest_call_ms"] is None else max(scan["newest_call_ms"], stamp)
            record["first"] = stamp if record["first"] is None else min(record["first"], stamp)
            if record["last"] is None or stamp >= record["last"]:
                record["last"] = stamp
                record["last_status"] = item.get("status") or "unknown outcome"
            if stamp >= recent_cutoff_ms:
                record["recent_count"] += 1
        tool_name = item.get("tool")
        keep_recent_event(record, stamp, tool_name, item.get("status"))
        if isinstance(tool_name, str) and tool_name:
            tool_record = record["tools"][tool_name]
            tool_record["count"] += 1
            if item.get("status") == "completed":
                tool_record["completed"] += 1
            if stamp is not None:
                tool_record["first"] = stamp if tool_record["first"] is None else min(tool_record["first"], stamp)
                if tool_record["last"] is None or stamp >= tool_record["last"]:
                    tool_record["last"] = stamp
                    tool_record["last_status"] = item.get("status") or "unknown outcome"
                if stamp >= recent_cutoff_ms:
                    tool_record["recent_count"] += 1
            hint = item.get("readOnlyHint")
            if isinstance(hint, bool):
                tool_record["read_only_hints"].add(hint)
            arguments = item.get("arguments")
            target = arguments.get("target") if isinstance(arguments, dict) else None
            if tool_name == "open_in_codex" and isinstance(target, dict):
                target_type = target.get("type")
                if isinstance(target_type, str):
                    tool_record["target_types"][target_type] += 1
                    if stamp is not None and stamp >= recent_cutoff_ms:
                        tool_record["recent_target_types"][target_type] += 1
                if target_type == "file" and isinstance(target.get("path"), str):
                    filename = Path(target["path"]).name
                    if filename:
                        tool_record["target_names"].add(filename)
                elif target_type == "browser" and isinstance(target.get("url"), str):
                    try:
                        host = urlsplit(target["url"]).hostname
                    except ValueError:
                        host = None
                    if host:
                        tool_record["target_hosts"].add(host.lower())
    if coverage is not None:
        coverage.update(scan)
    return usage


def history_fields(history: dict) -> dict:
    """Convert internal per-tool counters into JSON/HTML-friendly values."""
    tools = {}
    for name, record in history.get("tools", {}).items():
        hints = sorted(record["read_only_hints"])
        tools[name] = {
            **record,
            "first_date": utc_date(record.get("first"), milliseconds=True),
            "last_date": utc_date(record.get("last"), milliseconds=True),
            "read_only_hint": hints[0] if len(hints) == 1 else None,
            "read_only_hint_varies": len(hints) > 1,
            "target_types": dict(record.get("target_types", {})),
            "recent_target_types": dict(record.get("recent_target_types", {})),
            "target_names": sorted(record.get("target_names", set())),
            "target_hosts": sorted(record.get("target_hosts", set())),
        }
    return tools


def combine_history_entries(usage: dict, keys: list[str]) -> dict:
    """Combine distinct app tool namespaces without duplicating shared calls."""
    combined = {"count": 0, "recent_count": 0, "completed": 0,
                "selected_folder_count": 0, "other_folder_count": 0,
                "unknown_folder_count": 0, "first": None, "last": None,
                "last_status": None, "tools": {}, "events": []}
    for key in sorted(set(keys)):
        record = usage.get(key, {})
        for field in ("count", "recent_count", "completed", "selected_folder_count",
                      "other_folder_count", "unknown_folder_count"):
            combined[field] += int(record.get(field, 0))
        first = record.get("first")
        last = record.get("last")
        if first is not None and (combined["first"] is None or first < combined["first"]):
            combined["first"] = first
        if last is not None and (combined["last"] is None or last >= combined["last"]):
            combined["last"] = last
            combined["last_status"] = record.get("last_status")
        combined["events"].extend(record.get("events", []))
        combined["events"].sort(key=lambda event: event[0], reverse=True)
        del combined["events"][5:]
        for tool_name, tool in record.get("tools", {}).items():
            target = combined["tools"].setdefault(tool_name, {
                "count": 0, "recent_count": 0, "completed": 0,
                "first": None, "last": None, "last_status": None,
                "read_only_hints": set(), "target_types": defaultdict(int),
                "recent_target_types": defaultdict(int), "target_names": set(),
                "target_hosts": set(),
            })
            for field in ("count", "recent_count", "completed"):
                target[field] += int(tool.get(field, 0))
            tool_first = tool.get("first")
            tool_last = tool.get("last")
            if tool_first is not None and (target["first"] is None or tool_first < target["first"]):
                target["first"] = tool_first
            if tool_last is not None and (target["last"] is None or tool_last >= target["last"]):
                target["last"] = tool_last
                target["last_status"] = tool.get("last_status")
            target["read_only_hints"].update(tool.get("read_only_hints", set()))
            target["target_names"].update(tool.get("target_names", set()))
            target["target_hosts"].update(tool.get("target_hosts", set()))
            for field in ("target_types", "recent_target_types"):
                for kind, count in tool.get(field, {}).items():
                    target[field][kind] += count
    return combined


def snapshot_inventory(path: Path, rows: list[dict]) -> tuple[list[str], str]:
    """Compare and update a local snapshot containing IDs and fingerprints, not secrets or descriptions."""
    fingerprints = {}
    tool_fingerprints = {}
    for row in rows:
        endpoint = None
        if row.get("url"):
            try:
                parsed = urlsplit(str(row["url"]))
                endpoint = (parsed.scheme.lower(), (parsed.hostname or "").lower())
            except ValueError:
                endpoint = "remote endpoint configured"
        elif row.get("command"):
            command = row["command"]
            if isinstance(command, (list, tuple)):
                executable = str(command[0]) if command else ""
            else:
                try:
                    executable = shlex.split(str(command))[0]
                except (ValueError, IndexError):
                    executable = ""
            endpoint = ("local process", Path(executable).name if executable else "unknown executable")
        material = {
            "status": row.get("status"),
            "endpoint": endpoint,
            "allowed_tools": row.get("allowed_tools"),
            "approval": row.get("approval"),
            "has_env": row.get("has_env"),
            "catalog": row.get("tool_catalog", []),
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        fingerprints[row["id"]] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        saved_tools = {}
        for tool in row.get("tool_catalog", []):
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                continue
            tool_material = {key: tool.get(key) for key in ("name", "title", "description", "annotations", "schema")}
            tool_encoded = json.dumps(tool_material, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
            saved_tools[tool["name"]] = hashlib.sha256(tool_encoded.encode("utf-8")).hexdigest()
        if saved_tools:
            tool_fingerprints[row["id"]] = saved_tools

    previous_entries = None
    previous_tools = None
    previous_schema = None
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if (isinstance(saved, dict) and saved.get("schema") in {1, 2}
                    and isinstance(saved.get("entries"), dict)):
                previous_entries = saved["entries"]
                previous_schema = saved["schema"]
                previous_tools = saved.get("tools") if isinstance(saved.get("tools"), dict) else None
        except (OSError, UnicodeError, json.JSONDecodeError):
            return [], "The saved change-tracking file could not be read, so it was left untouched."
        if previous_entries is None:
            return [], "This change-tracking file uses an unsupported format, so it was left untouched."

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 2,
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "entries": fingerprints,
        "tools": tool_fingerprints,
    }
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".tmp-", delete=False) as temp:
            temp_name = temp.name
            json.dump(payload, temp, indent=2)
            temp.write("\n")
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    if previous_entries is None:
        return [], "Change tracking started. Run the report again with this file later to see what changed."

    changes = []
    for key in sorted(fingerprints.keys() - previous_entries.keys()):
        changes.append(f"Added: {key}")
    for key in sorted(previous_entries.keys() - fingerprints.keys()):
        changes.append(f"Removed: {key}")
    for key in sorted(fingerprints.keys() & previous_entries.keys()):
        if fingerprints[key] != previous_entries[key]:
            changes.append(f"Changed: {key}")
    note = "Compared with the saved copy."
    if previous_schema == 1 or previous_tools is None:
        note += " Tool-list change tracking starts with this report."
    else:
        name_by_id = {row["id"]: str(row.get("name", row["id"])) for row in rows}
        for entry_id in sorted(tool_fingerprints.keys() | previous_tools.keys()):
            current = tool_fingerprints.get(entry_id, {})
            previous = previous_tools.get(entry_id, {})
            if not isinstance(current, dict) or not isinstance(previous, dict):
                continue
            connection_name = name_by_id.get(entry_id, entry_id.rsplit(":", 1)[-1])
            if entry_id not in previous_tools:
                changes.append(f"Saved tool list now available for {connection_name} ({len(current)} tools)")
                continue
            if entry_id not in tool_fingerprints:
                # No current catalogue is available to compare; do not call its tools removed.
                continue
            for tool_name in sorted(current.keys() - previous.keys()):
                changes.append(f"New saved tool for {connection_name}: {tool_name}")
            for tool_name in sorted(previous.keys() - current.keys()):
                changes.append(f"Tool no longer listed for {connection_name}: {tool_name}")
            for tool_name in sorted(current.keys() & previous.keys()):
                if current[tool_name] != previous[tool_name]:
                    changes.append(f"Saved tool details changed for {connection_name}: {tool_name}")
    return changes, note


def approval_summary(*settings: dict) -> str:
    """Report only explicit approval configuration; do not infer permissions."""
    default = next((s.get("default_tools_approval_mode") for s in settings
                    if isinstance(s, dict) and s.get("default_tools_approval_mode")), None)
    overrides = {}
    for setting in settings:
        per_tool = setting.get("tools", {}) if isinstance(setting, dict) else {}
        if isinstance(per_tool, dict):
            for tool_name, policy in per_tool.items():
                if isinstance(policy, dict) and policy.get("approval_mode"):
                    overrides[tool_name] = policy["approval_mode"]
    parts = [f"default: {default}" if default else "no default approval policy visible"]
    if overrides:
        parts.append("tool overrides: " + ", ".join(f"{n}={m}" for n, m in sorted(overrides.items())))
    return "; ".join(parts)


def safe_server_details(*settings: dict) -> list[tuple[str, str]]:
    """Expose useful MCP settings while never rendering credential values."""
    details: dict[str, str] = {}
    for setting in settings:
        if not isinstance(setting, dict):
            continue
        for key in ("cwd", "experimental_environment", "required", "startup_timeout_sec",
                    "startup_timeout_ms", "tool_timeout_sec"):
            value = setting.get(key)
            if isinstance(value, (str, int, float, bool)):
                details[key] = str(value)
        for key, label in (("scopes", "Requested OAuth scopes"),
                           ("disabled_tools", "Tools explicitly disabled")):
            value = setting.get(key)
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                details[label] = ", ".join(value) if value else "None listed"
        for key, label in (("env", "Environment variable names"),
                           ("http_headers", "Static HTTP header names"),
                           ("env_http_headers", "Environment-backed HTTP header names")):
            value = setting.get(key)
            if isinstance(value, dict):
                details[label] = ", ".join(sorted(str(name) for name in value)) or "None"
        env_vars = setting.get("env_vars")
        if isinstance(env_vars, list):
            names = [item if isinstance(item, str) else item.get("name")
                     for item in env_vars if isinstance(item, (str, dict))]
            names = [name for name in names if isinstance(name, str)]
            details["Allowed environment variable names"] = ", ".join(sorted(names)) or "None"
        if setting.get("bearer_token_env_var"):
            details["Bearer token environment variable"] = str(setting["bearer_token_env_var"])
        oauth = setting.get("oauth")
        if isinstance(oauth, dict):
            if oauth.get("client_id"):
                details["OAuth client ID configured"] = "Yes (value hidden)"
            if oauth.get("client_secret"):
                details["OAuth client secret configured"] = "Yes (value hidden)"
    return sorted(details.items())


def load_inventory(config_path: Path, codex_home: Path,
                   history_coverage: dict | None = None,
                   config_override: dict | None = None,
                   project_dir: Path | None = None) -> tuple[dict, list[dict]]:
    if config_override is not None:
        config = config_override
    else:
        try:
            raw = config_path.read_bytes()
            config = tomllib.loads(raw.decode("utf-8"))
        except FileNotFoundError:
            raise SystemExit(f"Config not found: {config_path}")
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise SystemExit(f"Could not read TOML config {config_path}: {exc}")

    coverage = history_coverage if history_coverage is not None else {}
    usage = history_usage(codex_home, coverage, project_dir)
    servers = config.get("mcp_servers", {})
    if not isinstance(servers, dict):
        servers = {}
    rows = []
    for name, settings in sorted(servers.items()):
        settings = settings if isinstance(settings, dict) else {}
        history = usage.get(name, {})
        first_seen = utc_date(history.get("first"), milliseconds=True)
        last_used = utc_date(history.get("last"), milliseconds=True)
        if settings.get("enabled") is False:
            status = "Disabled in config"
        elif settings.get("enabled") is True:
            status = "Enabled in config (runtime unverified)"
        else:
            status = "Config entry present (enabled state omitted; runtime unknown)"
        if history.get("count", 0) == 0:
            last_used = None
        rows.append({
            "id": f"global:{name}",
            "history_key": name,
            "scope": "Effective Codex settings" if config_override is not None else "User config",
            "name": name,
            "status": status,
            "install": "Unknown (Codex config has no install timestamp)",
            "first_seen": first_seen,
            "last_used": last_used,
            "last_outcome": history.get("last_status"),
            "use_count": int(history.get("count", 0)),
            "recent_count": int(history.get("recent_count", 0)),
            "completed_count": int(history.get("completed", 0)),
            "recent_events": list(history.get("events", [])),
            "selected_folder_count": int(history.get("selected_folder_count", 0)),
            "other_folder_count": int(history.get("other_folder_count", 0)),
            "unknown_folder_count": int(history.get("unknown_folder_count", 0)),
            "tool_activity": history_fields(history),
            "command": settings.get("command"),
            "url": settings.get("url"),
            "allowed_tools": settings.get("enabled_tools", settings.get("allowed_tools")),
            "approval": approval_summary(settings),
            "has_env": bool(settings.get("env") or settings.get("env_vars") or settings.get("env_http_headers") or settings.get("bearer_token_env_var") or settings.get("http_headers")),
            "safe_details": safe_server_details(settings),
            "plugin_key": None,
        })

    # Plugin packages may bundle MCP servers in their .mcp.json manifest.
    # Plugin-level policy in config.toml can disable the plugin or override
    # individual server settings.
    plugin_configs = config.get("plugins", {})
    if not isinstance(plugin_configs, dict):
        plugin_configs = {}
    cache_root = codex_home / "plugins" / "cache"
    for manifest in sorted(cache_root.glob("*/*/*/.mcp.json")):
        try:
            package = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(package, dict):
            continue
        source, plugin_name = manifest.parents[2].name, manifest.parents[1].name
        plugin_key = f"{plugin_name}@{source}"
        plugin_settings = plugin_configs.get(plugin_key, {})
        plugin_config_present = plugin_key in plugin_configs
        remote_install_marker = (manifest.parents[1] / ".codex-remote-plugin-install.json").exists()
        if not isinstance(plugin_settings, dict):
            plugin_settings = {}
        definitions = package.get("mcpServers", {})
        if not isinstance(definitions, dict):
            continue
        for server_name, definition in sorted(definitions.items()):
            if not isinstance(definition, dict):
                definition = {}
            server_policies = plugin_settings.get("mcp_servers", {})
            policy = server_policies.get(server_name, {}) if isinstance(server_policies, dict) else {}
            if not isinstance(policy, dict):
                policy = {}
            effective_enabled = policy.get("enabled", definition.get("enabled", True))
            if plugin_settings.get("enabled") is False or effective_enabled is False:
                status = "Disabled by config"
            elif plugin_settings.get("enabled") is True:
                status = "Enabled by config (runtime unverified)"
            elif remote_install_marker:
                status = "Installed plugin cache (enabled state unknown)"
            elif plugin_config_present:
                status = "Plugin listed in config (enabled state unknown)"
            else:
                status = "Cache artifact only (install state unknown)"
            history = usage.get(server_name, {})
            first_seen = utc_date(history.get("first"), milliseconds=True)
            last_used = utc_date(history.get("last"), milliseconds=True) if history.get("count", 0) else None
            rows.append({
                "id": f"plugin:{plugin_key}:{server_name}",
                "history_key": server_name,
                "scope": f"Plugin {plugin_key}",
                "name": server_name,
                "status": status,
                "install": "Unknown (plugin cache does not provide a reliable install timestamp)",
                "first_seen": first_seen,
                "last_used": last_used,
                "last_outcome": history.get("last_status"),
                "use_count": int(history.get("count", 0)),
                "recent_count": int(history.get("recent_count", 0)),
                "completed_count": int(history.get("completed", 0)),
                "recent_events": list(history.get("events", [])),
                "selected_folder_count": int(history.get("selected_folder_count", 0)),
                "other_folder_count": int(history.get("other_folder_count", 0)),
                "unknown_folder_count": int(history.get("unknown_folder_count", 0)),
                "tool_activity": history_fields(history),
                "command": definition.get("command"),
                "url": definition.get("url"),
                "allowed_tools": policy.get("enabled_tools", definition.get("enabled_tools")),
                "approval": approval_summary(policy, definition),
                "has_env": bool(definition.get("env") or definition.get("env_vars") or definition.get("env_http_headers") or definition.get("bearer_token_env_var") or definition.get("http_headers")),
                "safe_details": safe_server_details(definition, policy),
                "plugin_key": plugin_key,
                "manifest_path": str(manifest),
            })

    # The cache can keep several versions of one plugin. They are one logical
    # MCP entry in Codex settings, and repeating the same inventory ID would
    # make both the report controls and the activity attribution ambiguous.
    plugin_rows: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("plugin_key") is not None and row.get("kind") != "connector":
            plugin_rows[row["id"]].append(row)
    redundant: set[int] = set()
    detail_fields = ("command", "url", "allowed_tools", "approval", "has_env", "safe_details")
    for group in plugin_rows.values():
        if len(group) < 2:
            continue
        primary = group[0]
        primary["manifest_paths"] = sorted({item["manifest_path"] for item in group})
        if any(any(item.get(field) != primary.get(field) for field in detail_fields)
               for item in group[1:]):
            # No saved source identifies the active version. Do not present
            # version-specific connection details as though they were current.
            primary.update(command=None, url=None, allowed_tools=None,
                           approval="Different saved plugin versions have different settings",
                           has_env=any(bool(item.get("has_env")) for item in group),
                           safe_details=[])
            primary["version_note"] = ("Several saved versions of this plugin have different MCP settings; "
                                       "the active version could not be identified.")
        if len({item["status"] for item in group}) > 1 and not all(
                item["status"].startswith("Disabled") for item in group):
            primary["status"] = "Saved plugin versions have different settings; current state unknown"
        redundant.update(id(item) for item in group[1:])
    if redundant:
        rows = [row for row in rows if id(row) not in redundant]

    # Plugin app manifests declare app-backed connectors (often backed by the
    # shared codex_apps MCP server). These are distinct from standalone MCP
    # servers. A cached manifest proves the plugin package is present, while
    # config.toml indicates whether Codex has the plugin enabled; neither is
    # proof that remote authorization is currently valid.
    directory_by_id: dict[str, dict] = {}
    for directory_file in sorted((codex_home / "cache" / "codex_app_directory").glob("*.json")):
        try:
            directory = json.loads(directory_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        for app in directory.get("connectors", []) if isinstance(directory, dict) else []:
            if isinstance(app, dict) and isinstance(app.get("id"), str):
                directory_by_id[app["id"]] = app

    tool_catalog: dict[str, dict[str, dict]] = defaultdict(dict)
    for tools_file in sorted((codex_home / "cache" / "codex_apps_tools").glob("*.json")):
        try:
            catalog = json.loads(tools_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        for tool in catalog.get("tools", []) if isinstance(catalog, dict) else []:
            if not isinstance(tool, dict):
                continue
            connector_id = tool.get("connector_id")
            tool_definition = tool.get("tool")
            name = tool_definition.get("name") if isinstance(tool_definition, dict) else None
            if isinstance(connector_id, str) and isinstance(name, str):
                annotations = tool_definition.get("annotations", {})
                tool_catalog[connector_id][name] = {
                    "name": name,
                    "title": tool_definition.get("title", ""),
                    "description": tool_definition.get("description", ""),
                    "annotations": annotations if isinstance(annotations, dict) else {},
                    "schema": tool_definition.get("inputSchema", {}),
                }

    # Report app manifests for plugins explicitly configured or carrying the
    # local installed-plugin marker. Do not mistake the global connector
    # directory (a catalogue of available apps) for the user's connections.
    for manifest in sorted(cache_root.glob("*/*/*/.app.json")):
        try:
            package = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(package, dict):
            continue
        source, plugin_name = manifest.parents[2].name, manifest.parents[1].name
        plugin_key = f"{plugin_name}@{source}"
        plugin_settings = plugin_configs.get(plugin_key)
        installed_marker = (manifest.parents[1] / ".codex-remote-plugin-install.json").exists()
        if not isinstance(plugin_settings, dict) and not installed_marker:
            continue
        plugin_settings = plugin_settings if isinstance(plugin_settings, dict) else {}
        enabled = plugin_settings.get("enabled")
        for slug, definition in (package.get("apps", {}).items()
                                 if isinstance(package.get("apps"), dict) else []):
            if not isinstance(definition, dict):
                continue
            app_id = definition.get("id")
            app = directory_by_id.get(app_id, {}) if isinstance(app_id, str) else {}
            name = app.get("name") or slug
            history_keys = [f"connector:{slug}"]
            if isinstance(app_id, str) and app_id:
                history_keys.append(f"connector-id:{app_id}")
            history = combine_history_entries(usage, history_keys)
            connector_tools = sorted(tool_catalog.get(app_id, {}).values(), key=lambda x: x["name"]) if isinstance(app_id, str) else []
            status = ("Plugin enabled; connector authorization/runtime unknown" if enabled is True
                      else "Plugin disabled in config" if enabled is False
                      else "Installed plugin; enabled/authorization state unknown" if installed_marker
                      else "Plugin configured; enabled state unknown")
            rows.append({
                "id": f"connector:{plugin_key}:{slug}",
                "app_slug": slug,
                "history_key": f"connector-id:{app_id}" if isinstance(app_id, str) and app_id else f"connector:{slug}",
                "history_keys": history_keys,
                "kind": "connector",
                "scope": f"App connector · plugin {plugin_key}",
                "name": name,
                "status": status,
                "install": "Unknown (no reliable connector install timestamp)",
                "first_seen": utc_date(history.get("first"), milliseconds=True),
                "last_used": utc_date(history.get("last"), milliseconds=True) if history.get("count", 0) else None,
                "last_outcome": history.get("last_status"),
                "use_count": int(history.get("count", 0)),
                "recent_count": int(history.get("recent_count", 0)),
                "completed_count": int(history.get("completed", 0)),
                "recent_events": list(history.get("events", [])),
                "selected_folder_count": int(history.get("selected_folder_count", 0)),
                "other_folder_count": int(history.get("other_folder_count", 0)),
                "unknown_folder_count": int(history.get("unknown_folder_count", 0)),
                "command": None,
                "url": None,
                "allowed_tools": None,
                "tool_catalog": connector_tools,
                "tool_activity": history_fields(history),
                "approval": "Connector authorization scopes are not exposed in local app manifests",
                "has_env": False,
                "safe_details": [],
                "plugin_key": plugin_key,
                "connector_id": app_id,
                "manifest_path": str(manifest),
                "manifest_entries": [(str(manifest), str(slug))],
            })

    # A connector may be declared by more than one installed plugin. Those
    # references share one Codex app ID, so show one app and preserve sources.
    app_groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("kind") == "connector" and isinstance(row.get("connector_id"), str):
            app_groups[row["connector_id"]].append(row)
    merged_ids: set[int] = set()
    for app_id, group in app_groups.items():
        if len(group) < 2:
            continue
        def source_priority(candidate: dict) -> tuple[bool, str]:
            slug = str(candidate["history_key"]).removeprefix("connector:")
            plugin_name = str(candidate["plugin_key"]).split("@", 1)[0]
            return (plugin_name != slug, str(candidate["plugin_key"]))
        primary = min(group, key=source_priority)
        history_keys = sorted({key for item in group for key in item.get("history_keys", [item["history_key"]])})
        primary["id"] = f"connector:{app_id}"
        primary["scope"] = f"App connector · referenced by {len(group)} installed plugins"
        primary["app_sources"] = sorted((str(item["plugin_key"]), str(item["status"])) for item in group)
        primary["manifest_paths"] = sorted({str(item["manifest_path"]) for item in group
                                            if item.get("manifest_path")})
        primary["manifest_entries"] = sorted({entry for item in group
                                              for entry in item.get("manifest_entries", [])})
        primary["history_keys"] = history_keys
        if len({item["status"] for item in group}) > 1:
            primary["status"] = "Plugin references have different settings; app state unknown"
        if len(history_keys) > 1:
            history = combine_history_entries(usage, history_keys)
            primary["first_seen"] = utc_date(history.get("first"), milliseconds=True)
            primary["last_used"] = utc_date(history.get("last"), milliseconds=True)
            primary["last_outcome"] = history.get("last_status")
            primary["use_count"] = int(history["count"])
            primary["recent_count"] = int(history["recent_count"])
            primary["completed_count"] = int(history["completed"])
            primary["recent_events"] = list(history.get("events", []))
            primary["selected_folder_count"] = int(history["selected_folder_count"])
            primary["other_folder_count"] = int(history["other_folder_count"])
            primary["unknown_folder_count"] = int(history["unknown_folder_count"])
            primary["tool_activity"] = history_fields(history)
        merged_ids.update(id(item) for item in group if item is not primary)
    rows = [row for row in rows if id(row) not in merged_ids]

    # A saved app setting may remain after its plugin files are removed, or an
    # account-backed app may be available outside this local plugin cache.
    # Show the setting as evidence without claiming a current account link.
    known_app_keys = {key for row in rows if row.get("kind") == "connector"
                      for key in (row.get("app_slug"), row.get("connector_id"))
                      if isinstance(key, str)}
    configured_apps = config.get("apps", {})
    if isinstance(configured_apps, dict):
        for slug, settings in sorted(configured_apps.items()):
            if slug == "_default" or slug in known_app_keys or not isinstance(settings, dict):
                continue
            history = usage.get(f"connector:{slug}", {})
            enabled = settings.get("enabled")
            status = ("Disabled in config" if enabled is False else
                      "Enabled in config (runtime unverified)" if enabled is True else
                      "App setting present (enabled state omitted; runtime unknown)")
            rows.append({
                "id": f"connector-config:{slug}",
                "app_slug": slug,
                "connector_id": None,
                "history_key": f"connector:{slug}",
                "history_keys": [f"connector:{slug}"],
                "kind": "connector",
                "scope": "Codex app setting",
                "name": slug.replace("_", " ").replace("-", " ").title(),
                "status": status,
                "install": "Unknown (setting has no install timestamp)",
                "first_seen": utc_date(history.get("first"), milliseconds=True),
                "last_used": utc_date(history.get("last"), milliseconds=True) if history.get("count", 0) else None,
                "last_outcome": history.get("last_status"),
                "use_count": int(history.get("count", 0)),
                "recent_count": int(history.get("recent_count", 0)),
                "completed_count": int(history.get("completed", 0)),
                "recent_events": list(history.get("events", [])),
                "selected_folder_count": int(history.get("selected_folder_count", 0)),
                "other_folder_count": int(history.get("other_folder_count", 0)),
                "unknown_folder_count": int(history.get("unknown_folder_count", 0)),
                "tool_activity": history_fields(history),
                "command": None,
                "url": None,
                "allowed_tools": None,
                "tool_catalog": [],
                "approval": "See Codex app controls if available",
                "has_env": False,
                "safe_details": [],
                "plugin_key": None,
            })

    # A tool prefix can be reused by different app IDs. Keep calls with an
    # explicit app ID, but do not guess where older prefix-only calls belong.
    prefix_owners: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        if row.get("kind") == "connector" and isinstance(row.get("connector_id"), str):
            for key in row.get("history_keys", []):
                if key.startswith("connector:"):
                    prefix_owners[key].add(row["connector_id"])
    for row in rows:
        if row.get("kind") != "connector":
            continue
        keys = row.get("history_keys", [row["history_key"]])
        ambiguous_keys = [key for key in keys if len(prefix_owners.get(key, set())) > 1]
        if not ambiguous_keys:
            continue
        row["history_keys"] = [key for key in keys if key not in ambiguous_keys]
        history = combine_history_entries(usage, row["history_keys"])
        row["first_seen"] = utc_date(history.get("first"), milliseconds=True)
        row["last_used"] = utc_date(history.get("last"), milliseconds=True)
        row["last_outcome"] = history.get("last_status")
        row["use_count"] = int(history["count"])
        row["recent_count"] = int(history["recent_count"])
        row["completed_count"] = int(history["completed"])
        row["recent_events"] = list(history.get("events", []))
        row["selected_folder_count"] = int(history["selected_folder_count"])
        row["other_folder_count"] = int(history["other_folder_count"])
        row["unknown_folder_count"] = int(history["unknown_folder_count"])
        row["tool_activity"] = history_fields(history)
        row["unattributed_prefix_calls"] = sum(int(usage.get(key, {}).get("count", 0)) for key in ambiguous_keys)

    # Calls identify a server or connector namespace, not a display name.
    # Only different entries sharing that same namespace have ambiguous calls.
    name_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        for key in row.get("history_keys", [row["history_key"]]):
            name_counts[key] += 1
    for row in rows:
        if any(name_counts[key] > 1 for key in row.get("history_keys", [row["history_key"]])):
            row["first_seen"] = None
            row["last_used"] = None
            row["use_count"] = 0
            row["recent_count"] = 0
            row["completed_count"] = 0
            row["recent_events"] = []
            row["tool_activity"] = {}
            row["history_note"] = "Ambiguous label; calls not attributed"
        row["history_scan_state"] = (
            "unavailable" if coverage["files_found"] == 0 and coverage["session_files_checked"] == 0 else
            "partial" if (coverage["files_unreadable"] or coverage["files_unsupported"]
                          or coverage["records_unreadable"] or coverage["session_files_unreadable"]
                          or coverage["session_records_unreadable"] or
                          (coverage["files_found"] == 0 and coverage["session_files_checked"] > 0)) else
            "checked"
        )
    return config, rows


def assess(row: dict) -> tuple[str, list[str]]:
    """Give evidence-based review notes in everyday language."""
    if row.get("kind") == "connector":
        return "Review prompts", [
            ("This app has a saved Codex setting, but local files cannot confirm whether an account is connected."
             if row.get("plugin_key") is None else
             "This app is listed by a Codex plugin, but local files cannot confirm whether an account is connected."),
            ("The tool list comes from a saved Codex catalogue; it may differ from what is available to your account now."
             if row.get("tool_catalog") else "No saved action list was found for this app."),
            "Check access in the app’s own account settings. Those permissions are not visible in this report.",
        ]
    command = str(row.get("command") or "")
    url = str(row.get("url") or "")
    flags = []
    if row.get("has_env"):
        flags.append("This tool has saved connection settings that may include login details. Their values are hidden here. If you do not recognize the tool, review or remove its entry in Codex settings.")
    if url:
        flags.append("This server uses a URL. Check who operates it and what information its tools may receive.")
    if command:
        flags.append("This starts a program on your computer. Check that you recognize the program and where it came from.")
    if row.get("allowed_tools") is not None:
        flags.append("Codex settings contain a tool list. Check that each listed action is needed and that sensitive changes ask you first.")
    else:
        flags.append("No tool list was visible in the settings checked, so this report cannot tell which actions the server offers.")
    flags.append("Information returned by a tool can include misleading instructions. Treat returned content as information to check, not as a command to follow.")
    return "Review prompts", flags


def app_enabled_for_selected_folder(config: dict | None, row: dict) -> bool | None:
    """Read an explicit app enabled setting from effective config for the chosen folder."""
    apps = config.get("apps") if isinstance(config, dict) else None
    if not isinstance(apps, dict):
        return None
    for key in (row.get("app_slug"), row.get("connector_id")):
        if isinstance(key, str) and isinstance(apps.get(key), dict):
            enabled = apps[key].get("enabled")
            if isinstance(enabled, bool):
                return enabled
            break
    defaults = apps.get("_default")
    enabled = defaults.get("enabled") if isinstance(defaults, dict) else None
    return enabled if isinstance(enabled, bool) else None


def friendly_status(row: dict) -> str:
    runtime = row.get("runtime_app")
    if row.get("kind") == "connector":
        selected = row.get("selected_folder_app_enabled")
        if selected is False:
            return "Off in Codex settings for this folder"
        if selected is True and (not isinstance(runtime, dict) or runtime.get("enabled") is False):
            return ("On in this folder's settings; off in Codex's general app list"
                    if isinstance(runtime, dict) else
                    "On in this folder's settings; Codex did not list this app")
    if row.get("kind") == "connector" and isinstance(runtime, dict):
        if runtime.get("enabled") is False:
            return "Off in Codex's general app list"
        if runtime.get("enabled") is True and runtime.get("callable") is True:
            return "On with actions listed in Codex's general app list"
        if runtime.get("enabled") is True:
            return "On in Codex's general app list; no actions listed there"
    if row.get("kind") == "connector" and row.get("runtime_checked"):
        return "Codex did not list this app in its general app list"
    status = row.get("status", "")
    if status == "Disabled in config" or status == "Disabled by config":
        if int(row.get("use_count", 0)):
            return "Marked off in settings; past requests remain in history"
        return "Marked off in the settings file checked"
    if status == "Enabled in config (runtime unverified)":
        return ("On in Codex settings; saved history shows a recent request" if int(row.get("recent_count", 0))
                else "On in Codex settings; running state not checked")
    if status == "Enabled by config (runtime unverified)":
        return ("On in Codex settings; saved history shows a recent request" if int(row.get("recent_count", 0))
                else "On in Codex settings; running state not checked")
    if status == "Plugin enabled; connector authorization/runtime unknown":
        return "Plugin setting is on; app account not checked"
    if status == "Installed plugin; enabled/authorization state unknown":
        return ("Plugin file found; app account not checked" if row.get("kind") == "connector"
                else "Plugin file found; server running state not checked")
    if status == "Installed plugin cache (enabled state unknown)":
        return "Saved plugin file found; no on/off setting found"
    if status == "Plugin listed in config (enabled state unknown)":
        return "Listed in Codex settings; no on/off setting found"
    if status == "Cache artifact only (install state unknown)":
        return "Plugin cache file found; installation not confirmed"
    if status == "Plugin configured; enabled state unknown":
        return "Listed in Codex settings; no on/off setting found"
    if status == "Plugin references have different settings; app state unknown":
        return "Several plugin files name this app; Codex did not confirm its current status"
    return "Listed in Codex settings; no on/off setting found"


def friendly_capabilities(row: dict) -> str:
    catalog = row.get("tool_catalog", [])
    if catalog:
        annotations = [tool.get("annotations", {}) for tool in catalog]
        not_readonly = any(a.get("readOnlyHint") is False for a in annotations)
        destructive = any(a.get("destructiveHint") is True for a in annotations)
        open_world = any(a.get("openWorldHint") is True for a in annotations)
        names = []
        for tool in catalog[:4]:
            raw_name = str(tool.get("title") or tool.get("name", "tool").split(".")[-1])
            names.append(raw_name.replace("_", " "))
        more = f" and {len(catalog) - len(names)} more" if len(catalog) > len(names) else ""
        parts = ["The saved tool list includes " + ", ".join(names) + more + "."]
        if destructive:
            parts.append("The saved tool list labels at least one action as potentially destructive.")
        elif not_readonly:
            parts.append("Some listed tools are not labelled read-only and may make changes.")
        elif annotations and all(a.get("readOnlyHint") is True for a in annotations):
            parts.append("The saved tool list labels these tools read-only; that label is only a hint.")
        else:
            parts.append("The saved tool list does not clearly say whether these tools can make changes.")
        if open_world:
            parts.append("At least one tool is labelled as reaching information beyond a fixed workspace.")
        return " ".join(parts)
    tools = row.get("allowed_tools")
    if isinstance(tools, list):
        if tools:
            return "Codex settings list these tool names: " + ", ".join(map(str, tools[:5])) + (f" and {len(tools)-5} more." if len(tools) > 5 else ".")
        return "The configured tool filter is empty."
    observed = sorted(row.get("tool_activity", {}))
    if observed:
        return "Tools seen in local history: " + ", ".join(observed[:5]) + (f" and {len(observed)-5} more." if len(observed) > 5 else ".")
    return ("No saved list of app actions was found." if row.get("kind") == "connector"
            else "No saved list of MCP server actions was found.")


def catalogue_review(row: dict) -> list[tuple[str, list[str]]]:
    """Flag tool descriptions that mention consequential actions; this is not a scanner verdict."""
    rules = [
        ("Descriptions mention changing or deleting information", re.compile(r"\b(create|write|edit|update|modify|delete|remove|send|post|publish|submit|move|rename|archive)\b", re.I)),
        ("Descriptions mention running code or commands", re.compile(r"\b(execute|run|shell|command|terminal|script|install|launch)\b", re.I)),
        ("Descriptions mention reading files or private information", re.compile(r"\b(read|list|search|fetch|download|file|folder|directory|email|message|document|record|database)\b", re.I)),
        ("Descriptions mention outside services", re.compile(r"\b(internet|web|browser|url|http|external|website|api|service)\b", re.I)),
    ]
    results = []
    for label, pattern in rules:
        matches = []
        for tool in row.get("tool_catalog", []):
            description = f"{tool.get('name', '')} {tool.get('title', '')} {tool.get('description', '')}"
            if pattern.search(description):
                matches.append(str(tool.get("title") or tool.get("name") or "Unnamed tool"))
        if matches:
            results.append((label, matches[:6]))
    return results


def friendly_action_summary(row: dict) -> str:
    """Describe only what saved tool text mentions, never a verified capability."""
    reviews = catalogue_review(row)
    if not reviews:
        return "No saved tool descriptions available to summarize."
    return "; ".join(f"{label.removeprefix('Descriptions mention ')} ({', '.join(names)})"
                      for label, names in reviews)


def friendly_activity(row: dict) -> str:
    if row.get("history_note"):
        return "Two entries share the same recorded tool name, so requests cannot be assigned to either one."
    if row.get("unattributed_prefix_calls") and not row.get("use_count"):
        return "Some saved requests used a name shared by different apps; none could be assigned to this app with confidence."
    if row.get("last_used"):
        try:
            stamp = dt.datetime.strptime(row["last_used"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=dt.timezone.utc)
            local = (stamp.astimezone().strftime("%d %b %Y at %I:%M %p").lstrip("0")
                     .replace(" at 0", " at ").replace("AM", "am").replace("PM", "pm"))
        except (TypeError, ValueError, OverflowError):
            local = "date unavailable"
        completed = int(row.get("completed_count", 0))
        count = int(row.get("use_count", 0))
        completion_note = f" ({completed} marked complete)" if completed else ""
        return f"Last recorded request: {local}. {count} matching requests found{completion_note}."
    if int(row.get("use_count", 0)) > 0:
        count = int(row["use_count"])
        request_word = "request" if count == 1 else "requests"
        return f"Codex made {count} recorded {request_word}, but their dates are unavailable."
    if row.get("history_scan_state") == "unavailable":
        return "Activity could not be checked: no saved Codex history files were found."
    if row.get("history_scan_state") == "partial":
        return "Activity check incomplete: some saved history files or records could not be checked."
    return "0 matching requests in the saved Codex history checked."


def compact_activity(row: dict) -> str:
    if row.get("history_note"):
        return "Matching requests could not be assigned to this entry"
    count = int(row.get("use_count", 0))
    if count:
        result = f"{count:,} recorded request{'s' if count != 1 else ''}"
        if row.get("last_used"):
            try:
                stamp = dt.datetime.strptime(row["last_used"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=dt.timezone.utc)
                result += "; latest " + stamp.astimezone().strftime("%d %b %Y").lstrip("0")
            except (TypeError, ValueError, OverflowError):
                pass
        return result
    if row.get("unattributed_prefix_calls"):
        return "Some requests share this app name; none could be assigned confidently"
    if row.get("history_scan_state") != "checked":
        return "Activity check incomplete"
    return "No matching request in saved history checked"


def terminal_text(value: object) -> str:
    """Keep names from local files from changing the terminal display."""
    cleaned = "".join(" " if unicodedata.category(char).startswith("C") else char
                      for char in str(value))
    collapsed = " ".join(cleaned.split())
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return collapsed.encode(encoding, errors="replace").decode(encoding, errors="replace")
    except LookupError:
        return collapsed


def terminal_unicode_supported() -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "█═─╔╗╚╝".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


class TerminalCompanion:
    """A small, optional terminal animation while the local report is prepared."""

    LETTERS = {
        "M": ("10001", "11011", "10101", "10001", "10001"),
        "C": ("01111", "10000", "10000", "10000", "01111"),
        "P": ("11110", "10001", "11110", "10000", "10000"),
        "L": ("10000", "10000", "10000", "10000", "11111"),
        "E": ("11111", "10000", "11110", "10000", "11111"),
        "N": ("10001", "11001", "10101", "10011", "10001"),
        "S": ("01111", "10000", "01110", "00001", "11110"),
        " ": ("000", "000", "000", "000", "000"),
    }

    def __init__(self, animate: bool = True) -> None:
        self.visible = sys.stdout.isatty()
        self.colour = (self.visible and "NO_COLOR" not in os.environ
                       and os.environ.get("TERM", "") != "dumb")
        self.animate = (self.visible and animate and "MCP_LENS_NO_ANIMATION" not in os.environ
                        and "CI" not in os.environ and os.environ.get("TERM", "") != "dumb")
        self.unicode = terminal_unicode_supported()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def _style(self, value: str) -> str:
        return f"\033[1;36m{value}\033[0m" if self.colour else value

    def _print_banner(self) -> None:
        width = shutil.get_terminal_size(fallback=(80, 24)).columns
        glyphs = [self.LETTERS[char] for char in "MCP LENS"]
        mark = "█" if self.unicode else "#"
        lines = ["  ".join("".join(mark if pixel == "1" else " " for pixel in glyph[row])
                           for glyph in glyphs).rstrip() for row in range(5)]
        print()
        if width >= max(map(len, lines)) + 2:
            for line in lines:
                print(self._style("  " + line))
        elif width >= 16:
            if self.unicode:
                print(self._style("  ╔══════════╗\n  ║ MCP LENS ║\n  ╚══════════╝"))
            else:
                print(self._style("  +----------+\n  | MCP LENS |\n  +----------+"))
        else:
            print(self._style("MCP LENS"))
        print(textwrap.fill("MCP Lens | What Codex lists on this device",
                            width=max(10, width - 2), initial_indent="  ", subsequent_indent="  "))
        print()

    def _run(self) -> None:
        faces = ("(o.o)", "(o.o)", "(-.-)", "(o.o)", "(o.o)", "(o.O)")
        index = 0
        while not self.stop_event.is_set():
            message = f"  /\\_/\\ {faces[index % len(faces)]}"
            width = shutil.get_terminal_size(fallback=(80, 24)).columns
            sys.stdout.write("\r\033[2K" + self._style(message[:max(1, width - 1)]))
            sys.stdout.flush()
            index += 1
            self.stop_event.wait(0.28)

    def __enter__(self) -> TerminalCompanion:
        if self.visible:
            self._print_banner()
            print("  Finding apps, MCP servers and saved activity...")
            if self.animate:
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()
            else:
                print("  /\\_/\\ (o.o)")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.thread is not None:
            self.stop_event.set()
            self.thread.join()
            sys.stdout.write("\r\033[2K")
        if self.visible:
            if exc_type is None:
                print("  /\\_/\\ (o.o)  Report ready." if self.thread is not None else "  Report ready.")
            else:
                print("  Check stopped.")


def terminal_status(row: dict) -> str:
    """Shorten verified status wording for the narrow terminal view."""
    status = friendly_status(row)
    simpler = {
        "Off in Codex settings for this folder": "Off in this folder's Codex settings",
        "On in this folder's settings; off in Codex's general app list":
            "On for this folder; off in Codex's app list",
        "On in this folder's settings; Codex did not list this app":
            "On for this folder; not listed in Codex's app list",
        "Off in Codex's general app list": "Off in Codex's app list",
        "On with actions listed in Codex's general app list":
            "On in Codex's app list; actions shown",
        "On in Codex's general app list; no actions listed there":
            "On in Codex's app list; no actions shown",
        "Codex did not list this app in its general app list":
            "Not listed in Codex's app list",
        "Marked off in settings; past requests remain in history":
            "Off in settings; earlier requests found",
        "Marked off in the settings file checked": "Off in settings",
        "On in Codex settings; saved history shows a recent request":
            "On in settings; a recent request was recorded",
        "On in Codex settings; running state not checked":
            "On in settings; running state unknown",
        "Plugin setting is on; app account not checked":
            "Plugin setting is on; account connection unknown",
        "Plugin file found; app account not checked":
            "Found in plugin files; account connection unknown",
        "Plugin file found; server running state not checked":
            "Found in plugin files; running state unknown",
        "Saved plugin file found; no on/off setting found":
            "Found in saved plugin files; on/off unknown",
        "Listed in Codex settings; no on/off setting found":
            "Listed in settings; on/off unknown",
        "Plugin cache file found; installation not confirmed":
            "Found in plugin cache; installation unknown",
        "Several plugin files name this app; Codex did not confirm its current status":
            "Found in several plugin files; current status unknown",
    }
    return simpler.get(status, status)


def terminal_activity(row: dict) -> str:
    """Explain saved calls without implying that an account is connected."""
    if row.get("history_note"):
        return "Saved requests could not be matched to this entry"
    count = int(row.get("use_count", 0))
    if count:
        result = f"Codex tried to use this {count:,} time{'s' if count != 1 else ''}"
        if row.get("last_used"):
            try:
                stamp = dt.datetime.strptime(row["last_used"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=dt.timezone.utc)
                result += "; last seen " + stamp.astimezone().strftime("%d %b %Y").lstrip("0")
            except (TypeError, ValueError, OverflowError):
                pass
        if row.get("history_scan_state") != "checked":
            result += "; activity check incomplete"
        return result
    if row.get("unattributed_prefix_calls"):
        return "Some saved requests could not be matched to this app"
    if row.get("history_scan_state") != "checked":
        return "Saved activity could not be fully checked"
    return "No matching use in the saved history checked"


def print_terminal_summary(rows: list[dict], project_dir: Path, output: Path,
                           app_status_note: str) -> None:
    """Show the same observed facts as the HTML report in a compact Terminal view."""
    width = max(12, min(shutil.get_terminal_size(fallback=(80, 24)).columns, 100))
    colour = (sys.stdout.isatty() and "NO_COLOR" not in os.environ
              and os.environ.get("TERM", "") != "dumb")
    unicode = terminal_unicode_supported()
    rule = "─" if unicode else "-"
    strong_rule = "═" if unicode else "="

    def style(value: str, code: str) -> str:
        return f"\033[{code}m{value}\033[0m" if colour else value

    def line(value: object = "", indent: int = 0, colour_code: str | None = None) -> None:
        safe = terminal_text(value)
        wrapped = textwrap.wrap(safe, width=max(8, width - indent),
                                break_long_words=True, break_on_hyphens=False) or [""]
        for part in wrapped:
            print(" " * indent + (style(part, colour_code) if colour_code else part))

    def section(title: str, group: list[dict]) -> None:
        print()
        line(f"{title} ({len(group)})", colour_code="1;36")
        print(style(rule * width, "90"))
        if not group:
            line("None found.", 2)
            return
        for row in sorted(group, key=lambda item: (-int(item.get("use_count", 0)),
                                                    str(item.get("name", "")).casefold())):
            print()
            status = terminal_status(row)
            lower_status = status.lower()
            status_colour = ("1" if lower_status.startswith(("off", "marked off")) else
                             "1;32" if lower_status.startswith("on ")
                             and "; off" not in lower_status and "; not listed" not in lower_status
                             else "1;33")
            line(row.get("name") or "Unnamed entry", 2, "1")
            line(f"Status: {status}", 4, status_colour)
            line(f"Saved activity: {terminal_activity(row)}", 4)

    apps = [row for row in rows if row.get("kind") == "connector"
            and not row.get("reference_only")]
    used_apps = [row for row in apps if int(row.get("use_count", 0)) > 0]
    other_apps = [row for row in apps if int(row.get("use_count", 0)) == 0]
    servers = [row for row in rows if row.get("kind") != "connector"
               and row.get("plugin_key") != "codex-app-tools@openai-bundled"]
    built_in = sum(row.get("plugin_key") == "codex-app-tools@openai-bundled"
                   for row in rows)
    references = sum(bool(row.get("reference_only")) for row in rows)

    print()
    line("Your Codex apps and MCP servers", colour_code="1;36")
    print(style(strong_rule * width, "36"))
    line(f"Folder settings: {project_dir}", 2)
    line("Apps (connectors) are services Codex can use; MCP servers are added tools.", 2)
    line("Saved activity covers this device, across folders.", 2)
    section("Apps with saved activity", used_apps)
    section("Other apps found", other_apps)
    section("MCP servers found", servers)
    if references:
        print()
        line(f"{references} more app name{'s' if references != 1 else ''} found only in plugin files; "
             "their current Codex status is not confirmed.", 2, "33")
    if built_in:
        line(f"{built_in} built-in Codex feature{'s' if built_in != 1 else ''} "
             "also appear in the full report.", 2)
    print()
    if app_status_note != APP_STATUS_CHECKED_NOTE:
        line(app_status_note, 2, "33")
    line("On/off describes Codex settings or its app list; account sign-in is not checked.", 2)
    print(style(rule * width, "90"))
    line(f"Full private report: {output.name}", 2, "1;36")
    line(f"Saved in: {output.parent}", 2)


FRIENDLY_TOOL_NAMES = {
    "open_in_codex": "Opened something in Codex",
    "load_workspace_dependencies": "Checked which coding tools were available",
    "read_thread": "Read a saved Codex conversation",
    "list_threads": "Looked at the list of Codex tasks and conversations",
    "list_projects": "Looked at the list of Codex projects",
    "end_realtime_voice_call": "Ended a voice session",
}


def friendly_connection_name(row: dict) -> str:
    if row.get("plugin_key") == "codex-app-tools@openai-bundled" and row.get("name") == "codex_app":
        return "Codex features"
    return str(row.get("name", "Unnamed connection"))


def friendly_connection_type(row: dict) -> str:
    if row.get("plugin_key") == "codex-app-tools@openai-bundled" and row.get("name") == "codex_app":
        return "Built-in Codex feature"
    if row.get("kind") == "connector":
        return "App (connector) listed in Codex"
    if row.get("url"):
        return "MCP server reached at a URL"
    if row.get("command"):
        return "MCP server started on this device"
    return "MCP server"


def friendly_tool_name(tool_name: str, row: dict) -> str:
    if tool_name in FRIENDLY_TOOL_NAMES:
        return FRIENDLY_TOOL_NAMES[tool_name]
    for tool in row.get("tool_catalog", []):
        if tool.get("name") == tool_name and tool.get("title"):
            return str(tool["title"])
    short_name = tool_name.rsplit(".", 1)[-1]
    return short_name.replace("_", " ").strip().capitalize() or "Unlabelled action"


def recorded_actions(row: dict) -> list[str]:
    """Summarize recorded call names and safe, non-content targets."""
    actions = []
    for name, item in sorted(row.get("tool_activity", {}).items(), key=lambda pair: (-int(pair[1].get("count", 0)), pair[0])):
        count = int(item.get("count", 0))
        label = friendly_tool_name(name, row)
        if name == "open_in_codex":
            kinds = item.get("target_types", {})
            recent_kinds = item.get("recent_target_types", {})
            files = int(kinds.get("file", 0)) if isinstance(kinds, dict) else 0
            pages = int(kinds.get("browser", 0)) if isinstance(kinds, dict) else 0
            recent_files = int(recent_kinds.get("file", 0)) if isinstance(recent_kinds, dict) else 0
            recent_pages = int(recent_kinds.get("browser", 0)) if isinstance(recent_kinds, dict) else 0
            other = count - files - pages
            parts = []
            if files:
                names = item.get("target_names", [])
                distinct = len(names) if isinstance(names, list) else 0
                parts.append(f"asked Codex to open {files} file{'s' if files != 1 else ''} ({distinct} file name{'s' if distinct != 1 else ''} recorded; {recent_files} in the last 30 days)")
            if pages:
                hosts = item.get("target_hosts", [])
                hosts_text = f" on {', '.join(hosts[:4])}" if isinstance(hosts, list) and hosts else ""
                parts.append(f"asked Codex to open {pages} web page{'s' if pages != 1 else ''}{hosts_text} ({recent_pages} in the last 30 days)")
            if other > 0:
                parts.append(f"asked Codex to open {other} other items")
            times = f"{count} time" if count == 1 else f"{count} times"
            completed = int(item.get("completed", 0))
            outcome = f" ({completed} completed, {count - completed} not marked complete)"
            actions.append(f"{'; '.join(parts) or f'{count} recorded requests'}. {times}{outcome}")
            continue
        completed = int(item.get("completed", 0))
        recent = int(item.get("recent_count", 0))
        times = f"{count} time" if count == 1 else f"{count} times"
        outcome = f"; {completed} completed, {count - completed} not marked complete"
        actions.append(f"{label}: {times} ({recent} in the last 30 days{outcome})")
    return actions


def friendly_tool_access(row: dict) -> str:
    allowed = row.get("allowed_tools")
    if isinstance(allowed, list):
        return (f"Codex settings list {len(allowed)} action name" + ("s" if len(allowed) != 1 else "") +
                " in this MCP server's enabled-tool filter" if allowed else
                "The MCP server's enabled-tool filter is empty")
    if isinstance(allowed, dict):
        names = allowed.get("tool_names")
        if isinstance(names, list) and names:
            return f"Codex settings list {len(names)} action names in a tool filter"
        return "An action rule is set, but the list is not shown here"
    if row.get("kind") == "connector":
        current = row.get("runtime_tool_summaries")
        if isinstance(current, list):
            enabled = sum(tool.get("enabled") is True for tool in current)
            return f"Codex currently lists {enabled} enabled app actions; account access is not shown"
        count = len(row.get("tool_catalog", []))
        return (f"Codex has a saved list of {count} app actions; account access is not visible"
                if count else "App account access is not visible in the files checked")
    observed = len(row.get("tool_activity", {}))
    if observed:
        return f"History shows {observed} kinds of action, but the full allowed-action list is not visible here"
    return "No allowed-action list was found in the settings checked"


def friendly_access_limit(row: dict) -> str:
    """State the access boundary that local Codex evidence cannot cross."""
    if row.get("kind") == "connector":
        return ("Which outside account is signed in, what that account has granted, and which "
                "pages, files or records its actions can reach are not visible here.")
    if row.get("plugin_key") == "codex-app-tools@openai-bundled":
        return "Codex folder access and approval decisions can vary by conversation; this is not a full permission list."
    return ("The server's live tool list, whether it is running now, and which files or outside "
            "accounts it can reach are not verified here.")


def friendly_access_examples(row: dict) -> str:
    """Show named actions with their evidence source, never as confirmed data scope."""
    current = row.get("runtime_tool_summaries")
    if isinstance(current, list):
        names = [str(item.get("title")) for item in current
                 if isinstance(item, dict) and item.get("enabled") is True and item.get("title")]
        if names:
            return "Actions Codex lists now include " + ", ".join(names[:4]) + (f" and {len(names) - 4} more." if len(names) > 4 else ".")
    allowed = row.get("allowed_tools")
    if isinstance(allowed, list) and allowed:
        names = [str(item).replace("_", " ") for item in allowed if isinstance(item, str)]
        if names:
            return "Names in the Codex tool filter include " + ", ".join(names[:4]) + (f" and {len(names) - 4} more." if len(names) > 4 else ".")
    catalog = row.get("tool_catalog")
    if isinstance(catalog, list) and catalog:
        names = [str(item.get("title") or item.get("name")).replace("_", " ")
                 for item in catalog if isinstance(item, dict) and (item.get("title") or item.get("name"))]
        if names:
            return "Names in a saved action list include " + ", ".join(names[:4]) + (f" and {len(names) - 4} more." if len(names) > 4 else ".")
    return ""


def friendly_confirmation(row: dict) -> str:
    if row.get("kind") == "connector":
        policy = row.get("app_policy")
        defaults = row.get("app_default_policy")
        for source in (policy, defaults):
            if isinstance(source, dict) and source.get("default_tools_approval_mode") in {"prompt", "approve", "auto"}:
                mode = source["default_tools_approval_mode"]
                return ("Codex settings say to ask before app actions by default" if mode in {"prompt", "approve"}
                        else "Codex settings allow app actions without a default prompt; individual actions may still ask")
        return "No app confirmation rule was found in the settings checked"
    approval = str(row.get("approval", ""))
    if "tool overrides:" in approval:
        entries = [x for x in approval.split("tool overrides:", 1)[1].split(",") if "=" in x]
        count = len(entries)
        return f"Separate confirmation rules are set for {count} action{'s' if count != 1 else ''}"
    if "default:" in approval and "no default" not in approval:
        return "A general confirmation rule is recorded in settings"
    return "No confirmation rule was found in the settings checked"


def evidence_source(row: dict) -> str:
    if row.get("kind") == "connector":
        if row.get("plugin_key") is None:
            return "Found in a Codex app setting. This does not confirm an account is connected."
        return "Found in a locally installed or configured plugin. This does not confirm an account is connected."
    if row.get("plugin_key"):
        return "Found in a plugin file or Codex settings. Activity is based on matching records in saved history."
    return "Found in the Codex settings file checked for this report."


def next_step(row: dict) -> str:
    if row.get("status") in {"Disabled in config", "Disabled by config"} and int(row.get("use_count", 0)):
        return "This is marked off in the settings checked. Saved requests may have happened before that setting changed."
    if row.get("kind") == "connector":
        if row.get("selected_folder_app_enabled") is False or row.get("status") == "Disabled in config":
            return "This app is marked off in Codex settings. Its outside account connection is a separate check."
        if row.get("runtime_checked") and not row.get("runtime_app"):
            return ("Codex did not return a current status for this app. "
                    "If you expect to use it, review it in Codex app settings.")
        if isinstance(row.get("runtime_app"), dict) and row["runtime_app"].get("enabled") is False:
            return "This app is off in Codex. Check the app’s own settings separately if you also want to revoke account access."
        if int(row.get("use_count", 0)):
            return "If you no longer need this app in Codex, review it in Codex app settings. Review the app’s own settings separately to change account access."
        if row.get("history_scan_state") != "checked" or row.get("unattributed_prefix_calls"):
            return "Activity could not be determined fully. Review this app in Codex settings if you no longer expect to use it."
        return ("No matching request was found in saved Codex history. If you do not expect to use this app, "
                "review it in Codex app settings. Review the app’s own settings separately to change account access.")
    if row.get("has_env"):
        return "If you do not recognize this tool, review it in Codex settings before using it again."
    if row.get("url"):
        try:
            scheme = urlsplit(str(row["url"])).scheme.lower()
        except ValueError:
            scheme = ""
        if scheme and scheme != "https":
            return "If you do not recognize this tool, review it in Codex settings before using it again."
        return "If you do not recognize this tool, review it in Codex settings before using it again."
    if row.get("command"):
        return "If you do not recognize this tool, review it in Codex settings before using it again."
    return "If you do not recognize this tool, review it in Codex settings before using it again."


def annotate_source_locations(rows: list[dict], config_path: Path,
                              layers: list[dict] | None = None) -> None:
    """Show only verified settings entries and plugin files, never setting values."""
    try:
        user_config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        user_config = {}
    for row in rows:
        locations: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def add(label: str, path: str, entry: str) -> None:
            if (path, entry) not in seen:
                locations.append({"label": label, "path": path, "entry": entry})
                seen.add((path, entry))

        if row.get("kind") == "connector":
            section = "apps"
            keys = {str(key) for key in (row.get("app_slug"), row.get("connector_id")) if key}
        elif row.get("plugin_key"):
            section = "plugins"
            keys = {str(row["plugin_key"])}
        else:
            section = "mcp_servers"
            keys = {str(row["name"])}

        for layer in layers or []:
            if not isinstance(layer, dict):
                continue
            name = layer.get("name", {})
            entries = layer.get("entries", {})
            if not isinstance(name, dict) or not isinstance(entries, dict):
                continue
            layer_keys = entries.get(section, [])
            if not isinstance(layer_keys, list):
                continue
            path = name.get("file")
            if name.get("type") == "project" and isinstance(name.get("dotCodexFolder"), str):
                path = str(Path(name["dotCodexFolder"]) / "config.toml")
            if not isinstance(path, str) or not path:
                continue
            for key in sorted(keys.intersection(str(k) for k in layer_keys)):
                label = "Ignored Codex settings entry" if layer.get("ignored") else "Codex settings entry"
                add(label, path, f"{section}.{key}")

        local_entries = user_config.get(section, {}) if isinstance(user_config, dict) else {}
        if isinstance(local_entries, dict):
            for key in sorted(keys.intersection(str(k) for k in local_entries)):
                add("Codex settings entry", str(config_path), f"{section}.{key}")

        manifest_entries = row.get("manifest_entries") or [
            (path, str(row.get("app_slug", ""))) for path in
            (row.get("manifest_paths") or ([row["manifest_path"]] if row.get("manifest_path") else []))]
        for path, slug in manifest_entries:
            if isinstance(path, str) and path:
                entry = (f"mcpServers.{row['name']}" if row.get("kind") != "connector"
                         else f"apps.{slug}" if slug else "app definition")
                add("Saved plugin file", path, entry)
        row["source_locations"] = locations


def action_guidance(row: dict, can_disable: bool, can_remove: bool) -> str:
    """Give the reader a concrete action matching the controls actually offered."""
    if row.get("plugin_key") == "codex-app-tools@openai-bundled":
        return ("This is part of Codex. It cannot be removed from this report. "
                "Saved requests may have happened before that setting changed."
                if "disabled" in str(row.get("status", "")).lower() and int(row.get("use_count", 0))
                else "This is part of Codex. It cannot be removed from this report.")
    if row.get("kind") == "connector":
        if can_disable:
            return ("Use Turn off in Codex below to save an off setting on this computer. "
                    "The report then checks whether it took effect for this folder. "
                    "To disconnect an outside account, use the separate account step below.")
        if row.get("selected_folder_app_enabled") is False or row.get("status") == "Disabled in config":
            return ("This app is already marked off in the Codex settings checked. "
                    + ("Past requests may have happened before that setting changed. "
                       if int(row.get("use_count", 0)) else "") +
                    "To disconnect an outside account, use the account step below.")
        runtime = row.get("runtime_app")
        if isinstance(runtime, dict) and runtime.get("enabled") is False:
            return ("Codex's general app list shows this app off. That does not confirm its setting "
                    "in every folder or its outside account connection. Use the account step below to disconnect an outside account.")
        return ("This report cannot change this app's Codex setting. "
                "Use the account step below to disconnect an outside account.")
    if can_remove:
        if can_disable:
            return ("Use Turn off in Codex to keep this MCP server listed but off, or Remove from Codex "
                    "to delete its entry from this computer's Codex settings. Neither action uninstalls "
                    "software or disconnects an outside account.")
        return ("This MCP server is already marked off in the settings checked. Use Remove from Codex "
                "to delete its entry from this computer's Codex settings. This does not uninstall "
                "software or disconnect an outside account.")
    if row.get("plugin_key"):
        plugin_key = str(row["plugin_key"])
        plugin_name = plugin_key.rsplit("@", 1)[0]
        if can_disable:
            return (f"Use Turn off in Codex to save an off setting for this MCP server. To remove the plugin that provides "
                    f"it, type /plugins in Codex, find {plugin_name}, and choose Uninstall plugin if available. "
                    "That may remove other tools from the same plugin.")
        if "disabled" in str(row.get("status", "")).lower():
            return (f"This MCP server is already marked off in the settings checked. To remove its plugin, "
                    f"type /plugins in Codex, find {plugin_name}, and choose Uninstall plugin if available. "
                    "That may remove other tools from the same plugin.")
        return (f"Only a saved plugin file was found for this MCP server. To check whether it is "
                f"installed, type /plugins in Codex and look for {plugin_name}. Choose Uninstall plugin "
                "if it is installed and that option is available.")
    return ("This MCP server was found outside the personal Codex settings this report can edit. "
            "Ask the person who set up this folder or your Codex administrator to remove it.")


def write_private_html(output: Path, document: str) -> None:
    """Replace a local HTML file without exposing a partial or world-readable report."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output.parent,
                                         prefix=output.name + ".tmp-", delete=False) as temp:
            temp_name = temp.name
            os.chmod(temp_name, 0o600)
            temp.write(document)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_name, output)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def render_share_html(rows: list[dict], output: Path, coverage: dict) -> None:
    """Export counts only; no names, paths, descriptions, or call targets."""
    apps = [row for row in rows if row.get("kind") == "connector" and not row.get("reference_only")]
    references = [row for row in rows if row.get("reference_only")]
    own = [row for row in rows if row.get("plugin_key") == "codex-app-tools@openai-bundled"]
    mcps = [row for row in rows if row.get("kind") != "connector"
            and row.get("plugin_key") != "codex-app-tools@openai-bundled"]
    recorded = sum(int(row.get("use_count", 0)) > 0 for row in rows)
    incomplete = sum(row.get("history_scan_state") != "checked" for row in rows)
    generated = dt.datetime.now().astimezone().strftime("%d %b %Y at %I:%M %p").lstrip("0")
    document = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MCP Lens — shareable summary</title><style>body{{font:16px/1.5 system-ui,sans-serif;max-width:720px;margin:3rem auto;padding:0 1.25rem;color:#171717;background:#f8f8f8}}h1{{font-size:1.7rem}}dl{{display:grid;grid-template-columns:1fr auto;gap:.7rem;border-block:1px solid #ddd;padding:1.2rem 0}}dt,dd{{margin:0}}dd{{font-variant-numeric:tabular-nums;font-weight:600}}small{{color:#555}}</style>
<body><h1>MCP Lens summary</h1><p>Created {html.escape(generated)}. This copy contains counts only.</p>
<dl><dt>Apps (connectors) listed by Codex or named in settings</dt><dd>{len(apps)}</dd><dt>Apps only named by plugin files</dt><dd>{len(references)}</dd><dt>MCP servers named in settings or plugin files</dt><dd>{len(mcps)}</dd><dt>Built-in Codex features found</dt><dd>{len(own)}</dd><dt>History files checked</dt><dd>{int(coverage.get("files_checked", 0))}</dd><dt>Entries with matching saved requests</dt><dd>{recorded}</dd><dt>Entries whose activity check was incomplete</dt><dd>{incomplete}</dd></dl>
<p>These numbers describe the settings and saved history checked at the time of the report. Settings can depend on the folder chosen; activity covers saved Codex history across folders. The counts do not show account permissions or prove that an entry was never used.</p>
<small>No connection names, file names, addresses, settings paths, tool descriptions, or request targets are included.</small></body></html>'''
    write_private_html(output, document)


def render_local_permission_defaults(config: dict | None, effective: bool) -> str:
    """Explain visible local-command defaults without claiming live chat access."""
    config = config if isinstance(config, dict) else {}
    mode = config.get("sandbox_mode")
    mode = mode if isinstance(mode, str) else None
    mode_text = {
        "read-only": "Read-only local mode is set.",
        "workspace-write": "Workspace editing is set for local work.",
        "danger-full-access": "Broad local access is set.",
    }.get(mode, "Codex did not provide a local file-access mode in the settings checked.")
    approval = config.get("approval_policy")
    if isinstance(approval, dict):
        approval_text = "Custom command-approval rules are configured; this report does not interpret each rule."
    else:
        approval_text = {
            "on-request": "Codex is set to ask before some local actions.",
            "never": "Codex is set not to ask for local command approval.",
        }.get(approval if isinstance(approval, str) else None,
              "No simple command-approval default was visible.")
    workspace = config.get("sandbox_workspace_write")
    workspace = workspace if isinstance(workspace, dict) else {}
    roots = workspace.get("writable_roots")
    roots = [root for root in roots if isinstance(root, str)] if isinstance(roots, list) else []
    roots_html = ("<ul>" + "".join(f"<li><code>{html.escape(root)}</code></li>" for root in roots) + "</ul>"
                  if roots else "<p>No additional writable folders were listed in these settings.</p>")
    network = workspace.get("network_access")
    if mode == "workspace-write" and isinstance(network, bool):
        network_text = ("Local commands are allowed to use the network."
                        if network else "Local command network access is set to off.")
    else:
        network_text = "A local command network setting could not be confirmed for this mode."
    profile = config.get("default_permissions")
    profile_html = (f"<p><strong>Default permission profile:</strong> {html.escape(profile)}. "
                    "This report does not resolve that profile's full rules.</p>"
                    if isinstance(profile, str) and profile else "")
    source = ("Codex returned settings for the selected folder."
              if effective else "Only a settings file was read; folder-specific overrides may be missing.")
    return (
        '<details class="local-permissions"><summary>Local file and command defaults</summary>'
        f"<p>{html.escape(source)} These are defaults, not a record of what a particular chat was allowed to do. "
        "Apps and MCP servers have separate access.</p>"
        f"<p><strong>File access:</strong> {html.escape(mode_text)}</p>"
        f"<p><strong>Asking first:</strong> {html.escape(approval_text)}</p>"
        f"{profile_html}"
        f"<p><strong>Additional writable folders:</strong></p>{roots_html}"
        f"<p><strong>Command network:</strong> {html.escape(network_text)} "
        "This setting does not describe app, MCP, or browser connections.</p>"
        "</details>"
    )


def render_discovered_skills_and_hooks(discovery: dict | None) -> str:
    """Show App Server discovery results without skill contents or hook commands."""
    discovery = discovery if isinstance(discovery, dict) else {}
    skills = discovery.get("skills", [])
    hooks = discovery.get("hooks", [])
    skills = skills if isinstance(skills, list) else []
    hooks = hooks if isinstance(hooks, list) else []
    skill_state = discovery.get("skills_state", "unavailable")
    hook_state = discovery.get("hooks_state", "unavailable")

    def status(value: object) -> str:
        return ("Listed as on" if value is True else "Listed as off" if value is False
                else "On/off state not provided")

    skill_items = []
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        name = skill.get("name") if isinstance(skill.get("name"), str) else "Unnamed skill"
        description = skill.get("description")
        description = description[:240] + ("…" if len(description) > 240 else "") if isinstance(description, str) else ""
        path = skill.get("path")
        scope = skill.get("scope")
        plugin = skill.get("pluginId")
        details = [status(skill.get("enabled"))]
        if isinstance(scope, str):
            scope_name = {"user": "Your Codex setup", "repo": "This project",
                          "system": "Codex", "admin": "Workspace administrator"}.get(scope, scope)
            details.append(f"Source: {scope_name}")
        if isinstance(plugin, str) and plugin:
            details.append(f"Plugin: {plugin}")
        skill_items.append(
            f"<li><strong>{html.escape(name)}</strong><br>{html.escape(' · '.join(details))}"
            + (f"<br>{html.escape(description)}" if description else "")
            + (f"<br><small>Found at <code>{html.escape(path)}</code></small>"
               if isinstance(path, str) and path else "") + "</li>"
        )

    event_names = {
        "preToolUse": "Before a tool is used", "postToolUse": "After a tool is used",
        "permissionRequest": "When permission is requested",
        "preCompact": "Before a chat is shortened", "postCompact": "After a chat is shortened",
        "sessionStart": "When a session starts", "sessionEnd": "When a session ends",
        "userPromptSubmit": "When a prompt is sent", "stop": "When Codex stops",
        "subagentStart": "When a helper agent starts",
        "subagentStop": "When a helper agent stops",
        "interrupt": "When Codex is interrupted",
    }
    handler_names = {
        "command": "Can run a local command", "mcpTool": "Can call an MCP tool",
        "prompt": "Can run a prompt", "agent": "Can start an agent",
    }
    hook_items = []
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        event = hook.get("eventName")
        event = event if isinstance(event, str) else None
        event_text = event_names.get(event, event if event is not None else "Event not provided")
        handler_type = hook.get("handlerType")
        handler = handler_names.get(handler_type, "Action type not provided") if isinstance(handler_type, str) else "Action type not provided"
        source = hook.get("source")
        path = hook.get("sourcePath")
        trust = hook.get("trustStatus")
        details = [status(hook.get("enabled")), handler]
        if isinstance(source, str):
            source_name = {"user": "Your Codex setup", "project": "This project",
                           "plugin": "A plugin", "system": "Codex",
                           "mdm": "Workspace administrator",
                           "sessionFlags": "This session's settings",
                           "cloudRequirements": "Workspace requirements",
                           "cloudManagedConfig": "Managed workspace settings",
                           "legacyManagedConfigFile": "Older managed settings file",
                           "legacyManagedConfigMdm": "Older managed device settings",
                           "unknown": "Source not identified"}.get(source, source)
            details.append(f"Source: {source_name}")
        if isinstance(trust, str) and trust in {"modified", "untrusted"}:
            details.append(f"Codex trust status: {trust}")
        hook_items.append(
            f"<li><strong>{html.escape(event_text)}</strong><br>{html.escape(' · '.join(details))}"
            + (f"<br><small>Found at <code>{html.escape(path)}</code></small>"
               if isinstance(path, str) and path else "") + "</li>"
        )

    def finding(label: str, state: object, count: int, error_count: object) -> str:
        if state == "unavailable":
            return f"Codex could not provide its {label} list for this folder."
        suffix = (f" Codex reported {error_count} file error{'s' if error_count != 1 else ''}."
                  if isinstance(error_count, int) and error_count else "")
        singular = "skill" if label == "skills" else "hook"
        return f"Codex listed {count} {singular if count == 1 else label} for this folder." + suffix

    skill_finding = finding("skills", skill_state, len(skill_items), discovery.get("skills_errors"))
    hook_finding = finding("hooks", hook_state, len(hook_items), discovery.get("hooks_errors"))
    warnings = discovery.get("hooks_warnings")
    if isinstance(warnings, int) and warnings:
        hook_finding += f" Codex also reported {warnings} warning{'s' if warnings != 1 else ''}."
    return (
        '<details class="other-codex-features"><summary>Skills and hooks Codex found for this folder</summary>'
        "<p>A skill gives Codex instructions for a task. A hook can perform an action when an event happens. "
        "This list comes from Codex's discovery check for the selected folder; it does not show that a skill ran "
        "or a hook fired. Skill instructions and full hook commands are not copied into this report.</p>"
        f"<h3>Skills</h3><p>{html.escape(skill_finding)}</p>"
        + (f"<ul>{''.join(skill_items)}</ul>" if skill_items else "")
        + f"<h3>Hooks</h3><p>{html.escape(hook_finding)}</p>"
        + (f"<ul>{''.join(hook_items)}</ul>" if hook_items else "")
        + "</details>"
    )


def render_html(rows: list[dict], config_path: Path, output: Path,
               snapshot_changes: list[str] | None = None,
               snapshot_note: str | None = None,
               app_status_note: str | None = None,
               history_coverage: dict | None = None,
               config_note: str | None = None,
               demo: bool = False,
               project_dir: Path | None = None,
               file_changes: dict | None = None,
               discovery: dict | None = None,
               permission_config: dict | None = None,
               permissions_effective: bool = False) -> None:
    body_rows = []
    cards_by_group = {"used-apps": [], "available-apps": [], "other-apps": [],
                      "references": [], "mcp": [], "codex": []}
    try:
        user_config_text = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        user_config_text = ""
    sorted_rows = sorted(rows, key=lambda row: (-int(row.get("use_count", 0)),
                                                str(row.get("name", "")).casefold()))
    for row in sorted_rows:
        severity, flags = assess(row)
        endpoint = "App-backed connector" if row.get("kind") == "connector" else "Not present in config"
        if row.get("url"):
            try:
                parsed = urlsplit(str(row["url"]))
                host = parsed.hostname
                endpoint = f"{parsed.scheme}://{host or 'host unavailable'}" if parsed.scheme else "URL configured (redacted)"
            except ValueError:
                endpoint = "URL configured (could not safely parse)"
        elif row.get("command"):
            command = row["command"]
            if isinstance(command, (list, tuple)):
                command = command[0] if command else ""
            else:
                try:
                    command = shlex.split(str(command))[0]
                except (ValueError, IndexError):
                    command = ""
            endpoint = f"Local process: {Path(str(command)).name}" if command else "Local process configured"
        tools = row.get("allowed_tools")
        if isinstance(tools, list):
            tools_text = ", ".join(str(x) for x in tools) if tools else "Empty tool filter"
        elif isinstance(tools, dict):
            names = tools.get("tool_names")
            tools_text = (", ".join(str(x) for x in names) or "Policy filter configured"
                          if isinstance(names, list) else "Policy filter configured")
        else:
            tools_text = "Not specified"
        tool_lines = []
        for tool_name, activity in sorted(row.get("tool_activity", {}).items()):
            hint = ("read-only hint: yes" if activity.get("read_only_hint") is True else
                    "read-only hint: no" if activity.get("read_only_hint") is False else
                    "read-only hint varied" if activity.get("read_only_hint_varies") else "read-only hint: unknown")
            detail = f"{activity['count']} calls; {activity['completed']} completed; last observed {activity.get('last_date') or 'unknown'} ({activity.get('last_status') or 'unknown outcome'}); {hint}"
            tool_lines.append(f"<li><code>{html.escape(tool_name)}</code>: {html.escape(detail)}</li>")
        catalog_lines = []
        for tool in row.get("tool_catalog", []):
            annotations = tool.get("annotations", {})
            hints = []
            for key, label in (("readOnlyHint", "read-only"), ("destructiveHint", "destructive"), ("openWorldHint", "open-world")):
                value = annotations.get(key)
                hints.append(f"{label} hint: {str(value).lower() if isinstance(value, bool) else 'unknown'}")
            desc = str(tool.get("description") or "")
            if len(desc) > 360:
                desc = desc[:357] + "..."
            catalog_lines.append(
                f"<li><code>{html.escape(str(tool.get('name', 'unknown')))}</code> "
                f"<small>({html.escape('; '.join(hints))})</small>"
                f"<br>{html.escape(desc)}</li>"
            )
        tool_sections = []
        if tool_lines:
            tool_sections.append("<strong>Observed calls</strong><ul>" + "".join(tool_lines) + "</ul>")
        if catalog_lines:
            tool_sections.append(
                f"<details><summary>Saved tool list: {len(catalog_lines)} tools</summary><ul>" +
                "".join(catalog_lines) + "</ul><small>Capability annotations are server-provided hints, not enforced permissions.</small></details>"
            )
        if not tool_sections:
            tool_sections.append("No per-tool activity or local schema available")
        behavior_review = catalogue_review(row)
        if behavior_review:
            behavior_items = "".join(
                f"<li><strong>{html.escape(label)}:</strong> {html.escape(', '.join(names))}</li>"
                for label, names in behavior_review
            )
            tool_sections.append(
                "<div><strong>What the saved tool descriptions mention</strong><ul>" + behavior_items +
                "</ul><small>These are words in a saved tool list. They do not confirm what the tools can actually do.</small></div>"
            )
        tool_activity_html = "".join(tool_sections)
        label = ("App (connector)" if row.get("kind") == "connector" else
                 "Built-in Codex feature" if row.get("plugin_key") == "codex-app-tools@openai-bundled" else
                 "MCP server")
        section_kind = ("references" if row.get("reference_only") else
                        "used-apps" if row.get("kind") == "connector" and int(row.get("use_count", 0)) else
                        "available-apps" if row.get("kind") == "connector" and isinstance(row.get("runtime_app"), dict)
                        and row["runtime_app"].get("enabled") and row["runtime_app"].get("callable")
                        and row.get("history_scan_state") == "checked" and not row.get("unattributed_prefix_calls") else
                        "other-apps" if row.get("kind") == "connector" else
                        "codex" if row.get("plugin_key") == "codex-app-tools@openai-bundled" else "mcp")
        if row.get("history_note") or row.get("unattributed_prefix_calls") or (int(row.get("use_count", 0)) > 0 and not row.get("last_used")):
            activity_band = "ambiguous"
        elif row.get("last_used"):
            activity_band = "recent" if int(row.get("recent_count", 0)) else "older"
        elif row.get("history_scan_state") != "checked":
            activity_band = "unavailable"
        else:
            activity_band = "none"
        actions = recorded_actions(row)
        action_count = len(actions)
        main_actions = ("<ul>" + "".join(f"<li>{html.escape(action)}</li>" for action in actions[:3]) + "</ul>"
                        if actions else "")
        extra_actions = (f'<details><summary>See all {action_count} recorded action types</summary><ul>' +
                         "".join(f"<li>{html.escape(action)}</li>" for action in actions[3:]) + "</ul></details>"
                         if action_count > 3 else "")
        open_tool = row.get("tool_activity", {}).get("open_in_codex", {})
        opened_names = open_tool.get("target_names", []) if isinstance(open_tool, dict) else []
        file_names_detail = (f'<details><summary>See file names opened in Codex ({len(opened_names)} names)</summary>'
                             f'<p>{html.escape(", ".join(opened_names[:30]))}'
                             f'{f" and {len(opened_names) - 30} more" if len(opened_names) > 30 else ""}</p>'
                             '<small>Only file names are shown, not their folders. Opening a file does not prove its contents were reviewed.</small></details>'
                             if opened_names else "")
        action_limit_note = ('<p class="evidence">These records show which Codex actions were requested. A file being opened does not prove its contents were audited or changed.</p>'
                             if row.get("plugin_key") == "codex-app-tools@openai-bundled" else "")
        recent_calls = int(row.get("recent_count", 0))
        recent_call_label = f"{recent_calls} matching request" + ("" if recent_calls == 1 else "s") + " recorded"
        recent_html = (f'<p><strong>Last 30 days</strong><br>{html.escape(recent_call_label)} in the saved history checked</p>'
                       if int(row.get("use_count", 0)) else "")
        folder_html = ""
        if history_coverage is not None and int(row.get("use_count", 0)):
            here = int(row.get("selected_folder_count", 0))
            elsewhere = int(row.get("other_folder_count", 0))
            unknown = int(row.get("unknown_folder_count", 0))
            folder_html = ("<p><strong>Where the conversations started</strong><br>" +
                           html.escape(f"{here} in conversations started in the selected folder; "
                                       f"{elsewhere} in other folders; {unknown} with no known starting folder.") +
                           "</p>")
        action_summary = friendly_action_summary(row)
        search_text = " ".join((str(row.get("name", "")), str(row.get("scope", "")),
                                 friendly_capabilities(row), friendly_activity(row), next_step(row)))
        related_active = next((other for other in rows
                               if other is not row
                               and str(other.get("name", "")).casefold() == str(row.get("name", "")).casefold()
                               and int(other.get("use_count", 0)) > 0), None)
        related_note = (f"<p class=\"evidence\">A separate {html.escape(str(related_active['kind'] == 'connector' and 'app (connector)' or 'MCP server'))} named {html.escape(str(related_active['name']))} has matching activity. This entry is tracked separately.</p>"
                        if related_active is not None and int(row.get("use_count", 0)) == 0 else "")
        prefix_note = (f'<p class="evidence">{int(row["unattributed_prefix_calls"])} saved requests used a name shared by different apps, so they were not assigned to this app.</p>'
                       if row.get("unattributed_prefix_calls") else "")
        event_items = []
        for stamp, tool_name, status in row.get("recent_events", []):
            date_text = dt.datetime.fromtimestamp(stamp / 1000, dt.timezone.utc).astimezone().strftime("%d %b %Y, %I:%M %p").lstrip("0")
            action_text = friendly_tool_name(tool_name, row)[:90]
            outcome = ("marked complete" if status == "completed" else
                       "marked failed" if status == "failed" else "outcome not confirmed")
            event_items.append(f"<li><time>{html.escape(date_text)}</time> · {html.escape(action_text)} · {outcome}</li>")
        timeline_html = ("<div><strong>Latest recorded requests</strong><ol>" + "".join(event_items) +
                         "</ol><small>These are Codex call records, not a list of changes made in the app. Request contents are not shown.</small></div>"
                         if event_items else "")
        actions_html = (f'<div><strong>Actions in saved history</strong>{main_actions}{extra_actions}{file_names_detail}{action_limit_note}</div>'
                        if actions else "")
        current_actions = row.get("runtime_tool_summaries")
        action_state = ""
        if isinstance(current_actions, list):
            marked_on = sum(item.get("enabled") is True for item in current_actions)
            unknown_state = sum(not isinstance(item.get("enabled"), bool)
                                for item in current_actions)
            action_state = f"{marked_on} enabled of {len(current_actions)} listed."
            if unknown_state:
                action_state += (f" Codex did not give an on/off state for {unknown_state} "
                                 f"action{'s' if unknown_state != 1 else ''}.")
        current_actions_html = ("<p><strong>Actions Codex lists now</strong><br>" +
                                html.escape(action_state + " ") +
                                "This does not confirm access inside the app account.</p>"
                                if isinstance(current_actions, list) else "")
        app_controls = []
        policy = row.get("app_policy") if isinstance(row.get("app_policy"), dict) else {}
        defaults = row.get("app_default_policy") if isinstance(row.get("app_default_policy"), dict) else {}
        if row.get("kind") == "connector":
            for key, control_label in (("destructive_enabled", "Actions labelled destructive"),
                               ("open_world_enabled", "Actions labelled as reaching outside services")):
                value = policy.get(key, defaults.get(key))
                if isinstance(value, bool):
                    app_controls.append(f"{control_label}: {'allowed by the general Codex setting' if value else 'off in the general Codex setting'}")
            tools_policy = policy.get("tools")
            if isinstance(tools_policy, dict) and tools_policy:
                app_controls.append(f"Separate settings exist for {len(tools_policy)} app action{'s' if len(tools_policy) != 1 else ''}")
        app_controls_html = ("<p><strong>App controls found in Codex settings</strong><br>" +
                             html.escape("; ".join(app_controls)) +
                             ". Individual action rules may change the general setting.</p>"
                             if app_controls else "")
        safe_details_html = ("<div><strong>Other settings visible locally</strong><dl>" + "".join(
            f"<dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd>"
            for label, value in row.get("safe_details", [])
        ) + "</dl><small>Header names and environment variable names are shown; their values are hidden.</small></div>"
                             if row.get("safe_details") else "")
        app_sources_html = ("<div><strong>Why this app appears</strong><p>These plugin files name the same app:</p><ul>" +
                            "".join(f"<li><code>{html.escape(source)}</code>: {html.escape(status)}</li>"
                                    for source, status in row.get("app_sources", [])) + "</ul></div>"
                            if row.get("app_sources") else "")
        technical = (
            f"<dl><dt>Source</dt><dd>{html.escape(str(row['scope']))}</dd>"
            f"<dt>{'Saved plugin entry' if row.get('kind') == 'connector' and row.get('plugin_key') else 'Codex setting'}</dt><dd>{html.escape(str(row['status']))}</dd>"
            f"<dt>Install time</dt><dd>{html.escape(str(row['install']))}</dd>"
            f"<dt>Endpoint or program</dt><dd>{html.escape(endpoint)}</dd>"
            f"<dt>Tool filter in settings</dt><dd>{html.escape(tools_text)}</dd>"
            f"<dt>Approval setting</dt><dd>{html.escape(str(row.get('approval', 'Not visible')))}</dd></dl>"
            + safe_details_html + app_sources_html
            + f"<div><strong>Tools seen in history and saved tool list</strong>{tool_activity_html}</div>"
            f"<div><strong>Things to know</strong><ul>" +
            "".join(f"<li>{html.escape(flag)}</li>" for flag in flags) + "</ul></div>"
        )
        access_summary = friendly_tool_access(row)
        approval_summary_text = friendly_confirmation(row)
        example_text = friendly_access_examples(row)
        example_html = (f'<p>{html.escape(example_text)} <small>Action names do not show which data an account or server can reach.</small></p>'
                        if example_text else '')
        access_data = (f'<details class="access-panel"><summary>What this report can tell you about access</summary>'
                       f'<dl class="quick-facts"><dt>Codex-side actions</dt><dd>{html.escape(access_summary)}</dd>'
                       f'<dt>Asking first</dt><dd>{html.escape(approval_summary_text)}</dd></dl>'
                       f'{example_html}</details>')
        direct_config_present = (row.get("plugin_key") is None and row.get("kind") != "connector"
                                 and section_bounds_path(user_config_text, ("mcp_servers", str(row["name"]))) is not None)
        can_disable = (row.get("plugin_key") != "codex-app-tools@openai-bundled"
                       and "disabled" not in str(row.get("status", "")).lower()
                       and not str(row.get("status", "")).startswith("Cache artifact only")
                       and (row.get("plugin_key") is not None or row.get("kind") == "connector" or direct_config_present)
                       and not (row.get("kind") == "connector" and row.get("selected_folder_app_enabled") is False)
                       and not (row.get("kind") == "connector" and isinstance(row.get("runtime_app"), dict)
                                and row["runtime_app"].get("enabled") is False
                                and row.get("selected_folder_app_enabled") is not True))
        can_remove = direct_config_present
        source_locations = row.get("source_locations", [])
        if source_locations:
            source_items = "".join(
                f'<li>{html.escape(str(source["label"]))}: '
                f'<code>{html.escape(str(source["path"]))}</code>'
                f'<br>Entry: <code>{html.escape(str(source["entry"]))}</code></li>'
                for source in source_locations)
            source_note = ("A plugin file names this app or MCP server. The file does not show whether it is active or linked to an account."
                           if any(source["label"] == "Saved plugin file" for source in source_locations) else
                           "This entry appears in more than one Codex settings file. Codex combines settings for the selected folder."
                           if len(source_locations) > 1 else "")
            plugin_names = sorted({str(source).rsplit("@", 1)[0] for source, _ in row.get("app_sources", [])}
                                  or ({str(row["plugin_key"]).rsplit("@", 1)[0]}
                                      if row.get("plugin_key") and row.get("plugin_key") != "codex-app-tools@openai-bundled" else set()))
            plugin_route = (f'<p>To look for the plugin in Codex, type <code>/plugins</code> and search for '
                            f'{html.escape(", ".join(plugin_names))}.</p>' if plugin_names else '')
            runtime_source = ('<p>Codex also listed this app in its general app list when this report was made.</p>'
                              if row.get("kind") == "connector" and isinstance(row.get("runtime_app"), dict) else '')
            where_html = (f'<section class="answer where-found"><h3>Where did we find it?</h3>'
                          f'{runtime_source}<ul>{source_items}</ul>{plugin_route}'
                          f'{f"<small>{html.escape(source_note)}</small>" if source_note else ""}</section>')
        elif row.get("plugin_key") == "codex-app-tools@openai-bundled":
            where_html = ('<section class="answer where-found"><h3>Where did we find it?</h3>'
                          '<p>This feature comes with Codex. There is no separate plugin to uninstall.</p></section>')
        else:
            source_text = ('Codex listed this app in its general app list. No matching settings or plugin file was identified.'
                           if row.get("kind") == "connector" and isinstance(row.get("runtime_app"), dict) else
                           'The exact settings or plugin file could not be identified from the data checked.')
            where_html = (f'<section class="answer where-found"><h3>Where did we find it?</h3>'
                          f'<p>{html.escape(source_text)}</p></section>')
        if row.get("reference_only"):
            reference_status = ("Not in Codex's current app list" if row.get("runtime_checked")
                                else "Current app list was not checked")
            reference_text = ("Codex did not list this as a current app when the report was made. "
                              if row.get("runtime_checked") else
                              "The current Codex app list could not be checked. ")
            activity_status = ("Activity could not be checked" if row.get("history_scan_state") == "unavailable" else
                               "Activity check incomplete" if row.get("history_scan_state") == "partial" else
                               "No matching request in saved history checked")
            reference_band = ("unavailable" if row.get("history_scan_state") != "checked"
                              else "none")
            cards_by_group["references"].append(
                f'<details class="connection-card" data-activity="{reference_band}" '
                f'data-search="{html.escape(str(row.get("name", "")), quote=True).lower()}">'
                f'<summary class="entry-summary"><span class="entry-name"><strong>{html.escape(str(row["name"]))}</strong>'
                f'<small>Only mentioned by a plugin</small></span><span class="entry-status">{html.escape(reference_status)}</span>'
                f'<span class="entry-use">{activity_status}</span></summary>'
                '<div class="entry-body"><div class="entry-guide">'
                f'<section class="answer"><h3>What did we find?</h3><p>{html.escape(reference_text)}'
                f'{html.escape(activity_status)}. A plugin file names this app. That does not establish an account connection.</p></section>'
                f'{where_html}'
                '<section class="answer next-step"><h3>What can you do?</h3>'
                '<p>Check whether the named plugin is installed in Codex. If it is not installed, there is no confirmed Codex app entry to turn off here.</p></section>'
                '<section class="answer"><h3>What can’t we confirm?</h3>'
                '<p>Whether this app is connected to an outside account or can be used from Codex now.</p></section>'
                '</div></div></details>')
            continue
        action_attributes = (f'data-entry-id="{html.escape(str(row["id"]), quote=True)}" '
                             f'data-entry-name="{html.escape(friendly_connection_name(row), quote=True)}" '
                             f'data-entry-type="{html.escape(label, quote=True)}"')
        disable_button = (f'<button type="button" class="manage-button" {action_attributes} '
                          'data-action="disable" disabled>Turn off in Codex</button>' if can_disable else '')
        remove_button = (f'<button type="button" class="manage-button" {action_attributes} '
                         'data-action="remove" disabled>Remove from Codex</button>' if can_remove else '')
        manage_controls = (f'<div class="manage-controls">{disable_button}{remove_button}'
                           f'<span class="manage-file-note">To use these buttons, double-click Open MCP Lens.command in the MCP Lens folder and use the report it opens.</span>'
                           f'<span class="manage-result" role="status" aria-live="polite"></span>'
                           f'<small>This changes Codex settings only. It does not disconnect an outside account.</small></div>'
                           if can_disable or can_remove else '')
        account_controls = (
            '<div class="account-controls"><strong>To disconnect the outside account</strong>'
            '<p>MCP Lens cannot tell whether this app is connected to an account. Turning it off in Codex does not disconnect that account.</p>'
            '<a class="account-link" href="https://chatgpt.com/plugins" target="_blank" rel="noopener noreferrer">Open ChatGPT Plugins</a>'
            f'<small>Find {html.escape(friendly_connection_name(row))} under Installed. Use Disconnect if it is shown. '
            'If it is not shown there, check the connected apps or authorized apps page in that service’s own account settings. '
            'Uninstalling a plugin alone may leave an account connection in place.</small></div>'
            if row.get("kind") == "connector" else '')
        version_note = ('<p class="evidence">Saved copies of this plugin disagree about this MCP server. '
                        'MCP Lens could not tell which copy Codex is using.</p>'
                        if row.get("version_note") else '')
        found_html = (f'<section class="answer"><h3>What did we find?</h3>'
                      f'<p>{html.escape(friendly_status(row))}. {html.escape(friendly_activity(row))}</p>'
                      f'{version_note}{related_note}{prefix_note}</section>')
        action_html = (f'<section class="answer next-step"><h3>What can you do?</h3>'
                       f'<p>{html.escape(action_guidance(row, can_disable, can_remove))}</p>'
                       f'{manage_controls}{account_controls}</section>')
        limit_html = (f'<section class="answer"><h3>What can’t we confirm?</h3>'
                      f'<p>{html.escape(friendly_access_limit(row))}</p></section>')
        description_summary_html = (f'<p><strong>What saved descriptions say</strong><br>{html.escape(action_summary)}<br><small>Descriptions can be incomplete or out of date. This isn’t a check of what the tool actually did.</small></p>'
                                    if row.get("tool_catalog") else "")
        activity_details_html = (f'<details class="activity-details"><summary>See recorded activity</summary>'
                                 f'{recent_html}{folder_html}{timeline_html}{actions_html}</details>'
                                 if recent_html or folder_html or timeline_html or actions_html else '')
        cards_by_group[section_kind].append(
            f'<details class="connection-card" data-activity="{activity_band}" data-search="{html.escape(search_text, quote=True).lower()}">'
            f'<summary class="entry-summary"><span class="entry-name"><strong>{html.escape(friendly_connection_name(row))}</strong><small>{label}</small></span>'
            f'<span class="entry-status">{html.escape(friendly_status(row))}'
            f'{"<small>External account access not checked</small>" if row.get("kind") == "connector" else ""}</span>'
            f'<span class="entry-use">{html.escape(compact_activity(row))}</span></summary><div class="entry-body">'
            f'<div class="entry-guide">{found_html}{where_html}{action_html}{limit_html}</div>'
            f'{access_data}'
            f'{activity_details_html}'
            f'<details><summary>More details</summary><p><strong>Technical name</strong><br><code>{html.escape(str(row["name"]))}</code></p>'
            f'<p><strong>Connection type</strong><br>{html.escape(friendly_connection_type(row))}</p>'
            f'{current_actions_html}'
            f'{app_controls_html}'
            f'<p><strong>Action names found</strong><br>{html.escape(friendly_capabilities(row))}</p>'
            f'{description_summary_html}'
            f'<details><summary>Technical details</summary>{technical}</details></details></div></details>'
        )
        body_rows.append("<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in (
            label, row["scope"], row["name"], row["status"], row["install"],
            row.get("history_note", row["first_seen"] or "No matching use record"),
            (row["last_used"] + f" ({row['last_outcome']})") if row["last_used"] else row.get("history_note", "No matching call record"),
            f"{row['use_count']} calls; {row['completed_count']} completed", endpoint, tools_text,
            row.get("approval", "No approval policy visible"), "; ".join(flags),
            "See connection card for observed and catalogue tools"
        )) + "</tr>")
    if not body_rows:
        body_rows.append('<tr><td colspan="13">No MCP servers or app connectors found in the scanned config and plugin cache.</td></tr>')
    generated = dt.datetime.now().astimezone().strftime("%d %b %Y at %I:%M %p").lstrip("0")
    app_rows = [row for row in rows if row.get("kind") == "connector" and not row.get("reference_only")]
    own_rows = [row for row in rows if row.get("plugin_key") == "codex-app-tools@openai-bundled"]
    mcp_count = sum(row.get("kind") != "connector"
                    and row.get("plugin_key") != "codex-app-tools@openai-bundled"
                    for row in rows)
    available_apps = sum(bool(isinstance(row.get("runtime_app"), dict)
                              and row["runtime_app"].get("enabled")
                              and row["runtime_app"].get("callable")) for row in app_rows)
    used_apps = sum(int(row.get("use_count", 0)) > 0 for row in app_rows)
    app_status_checked = any(row.get("runtime_checked") for row in app_rows)
    app_metric = (f'<div class="metric"><strong>{available_apps}</strong><span>Apps on with actions listed by Codex</span>'
                  '<small>General app list; this folder and account sign-in may differ</small></div>'
                  if app_status_checked else '')
    coverage = history_coverage or {}
    files_checked = int(coverage.get("files_checked", 0))
    files_unreadable = int(coverage.get("files_unreadable", 0))
    files_unsupported = int(coverage.get("files_unsupported", 0))
    if not int(coverage.get("files_found", 0)):
        history_note = ("No history database was found; saved session logs provide a partial activity count."
                        if int(coverage.get("session_files_checked", 0)) else
                        "No saved Codex history files were found; activity could not be checked.")
    elif files_unreadable or files_unsupported or int(coverage.get("records_unreadable", 0)):
        history_note = (f"Read {files_checked} saved Codex history files. "
                        f"{files_unreadable} files could not be read, {files_unsupported} used an unsupported format, "
                        f"and {int(coverage.get('records_unreadable', 0))} records could not be read.")
    else:
        history_note = f"Read {files_checked} saved Codex history file{'s' if files_checked != 1 else ''}."
    if int(coverage.get("records_undated", 0)):
        undated = int(coverage["records_undated"])
        history_note += f" {undated} request{'s' if undated != 1 else ''} had no usable date."
    duplicates = int(coverage.get("records_duplicate", 0))
    if duplicates:
        history_note += f" {duplicates} duplicate call record{'s were' if duplicates != 1 else ' was'} counted once."
    session_checked = int(coverage.get("session_files_checked", 0))
    if session_checked:
        history_note += f" Compared {session_checked} saved session log{'s' if session_checked != 1 else ''} for missing calls."
    added_calls = int(coverage.get("session_calls_added", 0))
    if added_calls:
        history_note += f" Added {added_calls} call{'s' if added_calls != 1 else ''} from session logs that were absent from the history database."
    if int(coverage.get("session_files_unreadable", 0)) or int(coverage.get("session_records_unreadable", 0)):
        history_note += " Some saved session records could not be compared."
    oldest = coverage.get("oldest_call_ms")
    newest = coverage.get("newest_call_ms")
    if isinstance(oldest, int) and isinstance(newest, int):
        start = dt.datetime.fromtimestamp(oldest / 1000, dt.timezone.utc).astimezone().strftime("%d %b %Y").lstrip("0")
        end = dt.datetime.fromtimestamp(newest / 1000, dt.timezone.utc).astimezone().strftime("%d %b %Y").lstrip("0")
        history_note += f" Dated requests in those files range from {start} to {end}."
    history_note += " Activity covers saved Codex history across folders, not only the selected folder."
    unknown_folder = int(coverage.get("calls_with_unknown_folder", 0))
    if unknown_folder:
        history_note += f" {unknown_folder} call{'s' if unknown_folder != 1 else ''} could not be tied to a starting folder."
    changes_html = ""
    if snapshot_note:
        action_names = {"Added": "Added", "Removed": "Removed", "Changed": "Changed"}
        change_items = ""
        for change in snapshot_changes or []:
            if change.startswith(("New saved tool for ", "Tool no longer listed for ",
                                  "Saved tool details changed for ", "Saved tool list now available for ")):
                change_items += f"<li>{html.escape(change)}</li>"
                continue
            action, _, entry_id = change.partition(": ")
            current = next((row for row in rows if row.get("id") == entry_id), None)
            fallback = entry_id.rsplit(":", 1)[-1].replace("_", " ").replace("-", " ")
            name = str(current.get("name")) if current else fallback
            friendly_change = f"{action_names.get(action, 'Updated')}: {name}"
            change_items += f"<li>{html.escape(friendly_change)}</li>"
        if not change_items:
            change_items = "<li>No changes found since the saved copy.</li>" if snapshot_changes is not None and "tracking started" not in snapshot_note else ""
        changes_html = f'<div class="note"><strong>Changes since the saved copy</strong><br>{html.escape(snapshot_note)}' + (f"<ul>{change_items}</ul>" if change_items else "") + "</div>"
    scope_html = ""
    if project_dir is not None:
        scope_html = (f'<div class="note"><strong>Folder used for settings:</strong> '
                      f'<code>{html.escape(str(project_dir))}</code><br>'
                      'The connection list uses Codex settings for this folder. Saved connection activity covers all folders. '
                      'The file edits below are limited to paths inside this folder.</div>')
    permission_html = render_local_permission_defaults(permission_config, permissions_effective)
    discovery_html = render_discovered_skills_and_hooks(discovery)
    file_change_html = ""
    if file_changes is not None:
        file_lines = []
        for stamp, path, action in file_changes["events"]:
            when = (dt.datetime.fromtimestamp(stamp / 1000, dt.timezone.utc).astimezone()
                    .strftime("%d %b %Y at %I:%M %p").lstrip("0")) if stamp is not None else "Date unavailable"
            file_lines.append(f'<li><strong>{html.escape(action)}</strong> '
                              f'<code>{html.escape(path)}</code> · {html.escape(when)}</li>')
        file_count = int(file_changes["files"])
        edit_count = int(file_changes["count"])
        check = file_changes["coverage"]
        if not (check["database_checked"] or check["session_checked"]):
            finding = "Saved file edit history could not be checked on this device."
        elif edit_count:
            finding = f"{edit_count} recorded edit action{'s' if edit_count != 1 else ''} across {file_count} file{'s' if file_count != 1 else ''} in this folder."
        else:
            finding = "No Codex file edit record matched this folder in the saved history checked."
        partial = (check["database_unreadable"] or check["database_unsupported"] or
                   check["session_unreadable"] or check["records_unreadable"] or check["paths_unresolved"])
        partial_note = " Some saved records could not be checked or placed in a folder." if partial else ""
        file_change_html = (f'<details class="file-changes"><summary>File edits recorded by Codex in this folder</summary>'
                            f'<p>{html.escape(finding + partial_note)}</p>'
                            '<p>These are completed file edit items in saved Codex history, not a full record of everything Codex changed. '
                            'Commands and other tools can also change files. A later edit or undo may have replaced these changes. '
                            'Only file names and dates are shown here; file contents and diffs are not included.</p>'
                            + (f'<ol>{"".join(file_lines)}</ol><small>Showing the latest {len(file_lines)} recorded file actions.</small>'
                               if file_lines else '') + '</details>')
    group_sections = "".join(
        f'<section class="group" data-group="{key}"><h2>{title}</h2><p class="group-intro">{description}</p>'
        f'<div class="cards">{"".join(cards_by_group[key])}</div></section>'
        for key, title, description in (
            ("used-apps", "Apps (connectors) with recorded requests", "Saved records show at least one request to each app. Some requests may have failed."),
            ("available-apps", "Other apps (connectors) on in Codex's general list", "Codex lists actions for these apps. No matching request was found in the saved history checked. This folder's setting and outside account access are separate checks."),
            ("other-apps", "Apps (connectors) named in settings or plugin files", "Open each app to see the file that names it and whether Codex reported it on or off. A file alone does not prove an account is connected."),
            ("mcp", "MCP servers named in settings or plugin files", "These names come from Codex settings or plugin files. Open a server to see its source and on/off setting. A plugin file alone does not show whether a server is running."),
        ) if cards_by_group[key]
    ) if rows else '<p>No MCP or app entries were found in the settings and plugin files checked.</p>'
    references_html = (f'<details class="advanced" id="plugin-app-references"><summary>Apps only named by plugin files '
                       f'({len(cards_by_group["references"])})</summary>'
                       '<p class="group-intro">A plugin file names each app below. No Codex app setting or matching request '
                       'was found in the data read. These names are not counted as current Codex apps. '
                       'Open a name to see the plugin file and whether the current app list and history were checked.</p>'
                       f'<div class="cards">{"".join(cards_by_group["references"])}</div></details>'
                       if cards_by_group["references"] else "")
    advanced_html = (f'<details class="advanced" id="built-in-codex-features"><summary>Built-in Codex features</summary>'
                     f'<section class="group" data-group="codex"><div class="cards">{"".join(cards_by_group["codex"])}</div></section></details>'
                     if cards_by_group["codex"] else "")
    document = f'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MCP Lens local report</title>
<style>
*{{box-sizing:border-box}} body{{font:16px/1.5 system-ui,-apple-system,sans-serif;max-width:1088px;margin:0 auto;padding:0 1.25rem 3rem;color:#111;background:#f8f8f8}}
.viewer-bar{{height:48px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #e6e6e6;font-size:.9rem}} .viewer-bar span{{color:#666}} h1{{margin:1.9rem 0 .2rem;font-size:1.5rem;line-height:1.2;letter-spacing:-.02em}} h2{{margin:2rem 0 .45rem;font-size:1.05rem;line-height:1.3}} .sub{{color:#666;font-size:.85rem}} .note{{background:#fff;padding:.8rem 1rem;border:1px solid #e6e6e6;margin:1.1rem 0;border-radius:6px;font-size:.9rem}} .intro{{max-width:72ch;margin:1rem 0;color:#333}} .group-intro{{margin:0 0 .65rem;color:#555;font-size:.9rem}} .caution{{display:flex;align-items:flex-start;gap:.9rem;background:#fff7f6;border-left:4px solid #b42318;padding:1rem 1.2rem;margin:1.25rem 0}} .caution svg{{flex:none;width:25px;height:25px;margin-top:.1rem}} .caution h2{{margin:0 0 .35rem;color:#8e1f17;font-size:1rem}} .caution p{{margin:.35rem 0;max-width:75ch}} .caution p:last-child{{margin-bottom:0}}
.summary{{display:flex;flex-wrap:wrap;gap:0;margin:1.3rem 0 1.6rem;border-block:1px solid #dedede}} .metric{{min-width:170px;flex:1;padding:.75rem 1rem .75rem 0}} .metric strong{{display:block;font-size:1.5rem;line-height:1.2;font-weight:600;font-variant-numeric:tabular-nums}} .metric span{{display:block;color:#222;font-size:.9rem;font-weight:600}} .metric small{{display:block;max-width:30ch;color:#555;font-size:.8rem;line-height:1.4;margin-top:.2rem}}
.filters{{display:flex;flex-wrap:wrap;gap:.65rem;margin:1rem 0;align-items:center;font-size:.9rem}} .filters input,.filters select{{font:inherit;padding:.5rem .65rem;border:1px solid #c9c9c9;border-radius:6px;background:#fff;color:#111}} .filters input{{min-width:min(100%,265px)}} .result-count{{color:#666;font-size:.82rem}}
.cards{{display:grid;gap:0;margin:.6rem 0 1.5rem;border:1px solid #e6e6e6;border-radius:8px;overflow:hidden;background:#fff}} .connection-card{{margin:0;border-bottom:1px solid #eeeeee;background:#fff}} .connection-card:last-child{{border-bottom:0}} .connection-card p{{margin:.6rem 0}} .entry-summary{{cursor:pointer;display:grid;grid-template-columns:minmax(175px,1.1fr) minmax(210px,1.2fr) minmax(190px,1fr);gap:1rem;align-items:center;padding:.8rem 1rem;list-style:none;min-height:62px}} .entry-summary::-webkit-details-marker{{display:none}} .entry-summary:hover{{background:#f8f8f8}} .entry-name strong{{display:block;font-size:1rem;font-weight:600}} .entry-name small,.entry-status small{{display:block;color:#666;font-size:.78rem;font-weight:400}} .entry-status,.entry-use{{font-size:.87rem;font-weight:400}} .entry-use{{color:#555}} .entry-body{{padding:.1rem 1rem 1rem;border-top:1px solid #f0f0f0}} .entry-guide{{margin:0 0 .8rem}} .answer{{display:grid;grid-template-columns:minmax(140px,170px) minmax(0,1fr);gap:0 .9rem;padding:.8rem .2rem;border-bottom:1px solid #ededed}} .answer h3{{font-size:.9rem;line-height:1.4;margin:0;font-weight:650}} .answer>:not(h3){{grid-column:2;margin-top:0}} .answer>:last-child{{margin-bottom:0}} .answer small{{color:#555}} .quick-facts{{display:grid;grid-template-columns:minmax(120px,145px) 1fr;gap:.3rem .7rem;margin:.65rem 0}} .next-step{{background:#f7f7f7;padding-inline:.65rem}}
.guide{{border-top:1px solid #dedede;padding-top:.1rem;max-width:76ch}} .guide ol{{margin:.5rem 0;padding-left:1.35rem}} .guide li{{margin:.45rem 0}} .guide p{{margin:.75rem 0}} .access-panel{{background:#f7f8f7;border-left:2px solid #5c6d5e;padding:.65rem .8rem;margin:.8rem 0}} .access-panel .quick-facts{{margin:.45rem 0 0}}
.local-permissions,.other-codex-features{{background:#fff;border:1px solid #e6e6e6;border-radius:6px;padding:.75rem 1rem;margin:1rem 0;font-size:.9rem}} .local-permissions>summary,.other-codex-features>summary{{font-size:.95rem}} .local-permissions p,.other-codex-features p{{max-width:75ch;margin:.55rem 0}} .other-codex-features h3{{font-size:.9rem;margin:1rem 0 .25rem}} .other-codex-features ul,.local-permissions ul{{margin:.45rem 0;padding-left:1.35rem}} .other-codex-features li{{padding:.35rem 0;overflow-wrap:anywhere}}
.manage-controls{{display:flex;flex-wrap:wrap;align-items:center;gap:.5rem .8rem;margin:.8rem 0}} .manage-button{{display:none;font:inherit;font-size:.88rem;padding:.48rem .8rem;background:#fff;border:1px solid #777;border-radius:5px;color:#111;cursor:pointer}} body.manager-active .manage-button{{display:inline-block}} .manage-button:hover:not(:disabled){{background:#f0f0f0}} .manage-button:disabled{{cursor:not-allowed;opacity:.65}} .manage-controls small{{flex-basis:100%;font-size:.8rem}} .manage-file-note{{color:#555;font-size:.8rem}} .manage-result{{font-size:.85rem;font-weight:600}} body.manager-active .manage-file-note,body.manager-active .saved-copy-banner{{display:none}} body:not(.manager-active) .live-controls-banner{{display:none}}
.where-found ul{{padding-left:1.3rem}} .where-found li{{margin:.4rem 0}} .where-found code{{overflow-wrap:anywhere;word-break:break-word}}
.mode-banner{{margin:1rem 0;padding:.85rem 1rem;border:1px solid #d5d5d5;border-left:4px solid #555;background:#fff;border-radius:4px;font-size:.92rem}} .mode-banner strong{{display:block}} .mode-banner p{{margin:.25rem 0 0}}
.account-controls{{display:flex;flex-wrap:wrap;align-items:center;gap:.5rem .8rem;margin:.8rem 0;padding:.75rem .8rem;border:1px solid #e6e6e6;border-radius:6px;background:#fafafa}} .account-controls strong,.account-controls p,.account-controls small{{flex-basis:100%}} .account-controls p{{margin:0}} .account-controls small{{font-size:.8rem}} .account-link{{display:inline-block;padding:.48rem .8rem;border:1px solid #777;border-radius:5px;color:#111;background:#fff;font-size:.88rem;text-decoration:none}} .account-link:hover{{background:#f0f0f0;text-decoration:underline}}
details{{margin-top:.7rem}} summary{{cursor:pointer;font-weight:600;color:#111}} details li{{margin:.5rem 0}} code{{overflow-wrap:anywhere}} dl:not(.quick-facts){{display:grid;grid-template-columns:minmax(120px,180px) 1fr;gap:.35rem .8rem}} dt{{font-weight:600}} dd{{margin:0;overflow-wrap:anywhere}} .table-wrap{{overflow:auto}} table{{border-collapse:collapse;width:100%;font-size:.9rem}} th,td{{border-bottom:1px solid #e6e6e6;padding:.65rem;text-align:left;vertical-align:top}} th{{color:#666;font-size:.8rem;font-weight:600}}
footer{{margin-top:2rem;color:#666;font-size:.85rem}} small{{color:#666}} .connection-card[hidden],.group[hidden],.advanced[hidden]{{display:none}} :focus-visible{{outline:3px solid #111;outline-offset:2px}} @media(max-width:720px){{body{{padding:0 .9rem 2rem}}.entry-summary{{grid-template-columns:1fr;gap:.2rem;padding:.8rem}}.entry-status,.entry-use{{font-size:.84rem}}.answer{{grid-template-columns:1fr;gap:.2rem}}.answer>:not(h3){{grid-column:1}}.quick-facts{{grid-template-columns:1fr;gap:0}}.quick-facts dt{{margin-top:.3rem}}.summary{{display:grid;grid-template-columns:1fr 1fr}}}}
.entry-summary{{grid-template-columns:minmax(0,1.1fr) minmax(0,1.2fr) minmax(0,1fr)}} .entry-name strong,.entry-status,.entry-use{{overflow-wrap:anywhere}} @media(max-width:720px){{.entry-summary{{grid-template-columns:1fr}}}}
</style>
<body><div class="viewer-bar"><strong>MCP Lens</strong><span>{'Fictional sample report' if demo else 'Private report on this device'}</span></div><h1>{'Example apps and MCP servers in Codex' if demo else 'Apps and MCP servers found for Codex'}</h1><div class="sub">{'Illustration with fictional data' if demo else f'Report created {generated} · Settings checked at that time'}</div>
{'' if demo else '<div class="mode-banner saved-copy-banner"><strong>Saved report: viewing only</strong><p>To turn off or remove an eligible entry, double-click <strong>Open MCP Lens.command</strong> in the MCP Lens folder. Use the report that opens and keep its Terminal window open. This saved file cannot change Codex settings.</p></div><div class="mode-banner live-controls-banner"><strong>Local controls are available</strong><p>Open an entry below to use its Turn off or Remove button, where available. Keep the Terminal window open while using these controls.</p></div>'}
{'<div class="note"><strong>Sample only</strong><br>These names and requests are invented. This page did not inspect a real Codex account.</div>' if demo else ''}
<section class="caution" aria-labelledby="caution-title"><svg viewBox="0 0 32 32" aria-hidden="true" focusable="false"><path d="M16 2 31 29H1L16 2Z" fill="#b42318"/><path d="M16 10v10" stroke="white" stroke-width="3" stroke-linecap="round"/><circle cx="16" cy="25" r="1.8" fill="white"/></svg><div><h2 id="caution-title">Why review these entries?</h2><p>Apps (connectors) and MCP servers can let Codex read information or make changes for you. What each can do depends on its settings and the access you gave it. If you no longer recognise or need one, review it in Codex and, for an app, in that app’s account settings.</p><p>The triangle is a reminder to review the list. It does not mean MCP Lens found a problem.</p></div></section>
<section class="terms" aria-labelledby="terms-title"><h2 id="terms-title">What is listed here?</h2><p><strong>App (connector):</strong> an app Codex can use to reach a service such as Notion; being listed does not prove your account is connected. <strong>MCP server:</strong> a separate source of tools for Codex that may run on this device or at a URL. <strong>Plugin:</strong> a package that can provide an app or MCP server. <strong>Skill:</strong> saved instructions for a task. <strong>Hook:</strong> an action Codex can run when an event happens. <strong>Built-in Codex feature:</strong> a tool supplied by Codex itself, shown separately below.</p><p>This is a view of Codex on this computer. An app connected in ChatGPT may still be usable there even if it is off here or absent from this report.</p></section>
<details class="guide"><summary>How to read this report</summary><ol><li><strong>Name and type:</strong> each entry says whether it is an app (connector), an MCP server, or a built-in Codex feature.</li><li><strong>On in Codex:</strong> Codex lists an app as enabled and shows actions for it. That does not confirm an outside account is connected or that this folder allows the app.</li><li><strong>Activity:</strong> a recorded request means Codex asked it to do something in saved history. The request may have failed. No matching request is not proof it was never used.</li><li><strong>Find it:</strong> open an entry and read “Where did we find it?” for its exact settings or plugin file and, for plugins, where to look in Codex.</li><li><strong>Stop or remove:</strong> open an entry and read “What can you do?” The available buttons depend on how the entry was installed. Buttons work in the report opened by the launcher.</li><li><strong>Outside accounts:</strong> turning an app off in Codex does not disconnect the account it may use. Each app has a separate account step.</li></ol><p>The report cannot confirm which account, pages, files, or records an outside app or MCP server can reach.</p></details>
<p class="intro">The first two counts can include the same app: one counts saved requests, and the other counts apps Codex currently lists as on with actions. Saved activity on this device may be incomplete, so no matching request does not prove an app or MCP server was never used.</p>
<section class="summary" aria-label="Entry counts"><div class="metric"><strong>{used_apps}</strong><span>Apps (connectors) with recorded requests</span><small>At least one matching request in saved history</small></div>{app_metric}<div class="metric"><strong>{mcp_count}</strong><span>MCP servers named in settings or plugin files</span><small>Being named does not prove a server is running</small></div></section>
{scope_html}
{permission_html}
{file_change_html}
{changes_html}
<div class="filters"><label for="connection-search">Find an app or tool</label><input id="connection-search" type="search" placeholder="Try Notion or GitHub" autocomplete="off"><label for="activity-filter">Show</label><select id="activity-filter"><option value="all">All activity</option><option value="recent">Requests in the last 30 days</option><option value="older">Earlier requests</option><option value="none">0 matches in checked history</option><option value="ambiguous">Request could not be assigned or dated</option><option value="unavailable">History check incomplete</option></select><span id="result-count" class="result-count" aria-live="polite"></span></div>
{group_sections}
{discovery_html}
{references_html}
<details class="why"><summary>How this report was checked</summary><p>{html.escape(config_note or "Only the user settings file was checked.")} {html.escape(history_note)} {html.escape(app_status_note or "App enabled status was not checked.")}</p><p>{'This sample contains fictional names and activity.' if demo else 'Keep this report private: it may contain app names, file names, and settings from this device.'}</p></details>
{advanced_html}
<details><summary>Show the technical inventory table</summary><p>Configuration source: <code>{html.escape(str(config_path))}</code></p><div class="table-wrap"><table><thead><tr><th>Type</th><th>Source</th><th>Name</th><th>Status</th><th>Install time</th><th>First observed call</th><th>Last observed call</th><th>Matching call records</th><th>Endpoint / command</th><th>Configured tool filter</th><th>Approval policy</th><th>Things to review</th><th>Observed / catalogue tools</th></tr></thead><tbody>{''.join(body_rows)}</tbody></table></div></details>
<footer>{'Fictional example of the report layout.' if demo else 'Based on local Codex settings, plugin files, saved tool lists, and activity records. Tool descriptions can be out of date. Account access and live server status are not shown here.'}</footer>
<script>
const searchBox=document.getElementById('connection-search');
const activityFilter=document.getElementById('activity-filter');
const cards=[...document.querySelectorAll('.connection-card')];
const resultCount=document.getElementById('result-count');
function filterCards(){{const query=searchBox.value.trim().toLowerCase();const activity=activityFilter.value;let shown=0;for(const card of cards){{const matchesText=card.dataset.search.includes(query);const matchesActivity=activity==='all'||card.dataset.activity===activity;card.hidden=!(matchesText&&matchesActivity);if(!card.hidden)shown++;}}for(const group of document.querySelectorAll('.group')){{group.hidden=!group.querySelector('.connection-card:not([hidden])');}}for(const id of ['plugin-app-references','built-in-codex-features']){{const panel=document.getElementById(id);if(!panel)continue;const hasMatch=!!panel.querySelector('.connection-card:not([hidden])');panel.hidden=!hasMatch;if(hasMatch&&(query||activity!=='all'))panel.open=true;}}resultCount.textContent=`Showing ${{shown}} of ${{cards.length}} entries`;}}
searchBox.addEventListener('input',filterCards);activityFilter.addEventListener('change',filterCards);filterCards();
const managedView=location.protocol==='http:'&&location.hostname==='127.0.0.1'&&/^\\/s\\/[^/]+\\/report$/.test(location.pathname);
if(managedView){{document.body.classList.add('manager-active');try{{const previous=sessionStorage.getItem('mcp-lens-last-action');if(previous){{const banner=document.createElement('div');banner.className='mode-banner';banner.setAttribute('role','status');banner.textContent=previous;document.querySelector('.viewer-bar').after(banner);sessionStorage.removeItem('mcp-lens-last-action');}}}}catch(error){{}}for(const button of document.querySelectorAll('.manage-button')){{button.disabled=false;button.addEventListener('click',async()=>{{const name=button.dataset.entryName;const type=button.dataset.entryType;const action=button.dataset.action;const message=action==='remove'?`Remove ${{name}} (${{type}}) from Codex settings? This cannot be undone from the report.`:`Turn off ${{name}} (${{type}}) in Codex? This does not disconnect any outside account.`;if(!window.confirm(message))return;const result=button.parentElement.querySelector('.manage-result');button.disabled=true;result.textContent='Saving…';try{{const response=await fetch(location.pathname.replace(/\\/report$/,'/action'),{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id:button.dataset.entryId,action}})}});const answer=await response.json();result.textContent=answer.message||'Could not complete this action.';if(answer.refreshed){{try{{sessionStorage.setItem('mcp-lens-last-action',answer.message||'The report was refreshed.');}}catch(error){{}}window.setTimeout(()=>location.reload(),1600);}}else if(!answer.saved)button.disabled=false;}}catch(error){{result.textContent='Could not reach the local manager. Open the report with the launcher again.';button.disabled=false;}}}});}}}}
</script></body></html>'''
    write_private_html(output, document)


def build_demo_report(output: Path) -> None:
    """Render the normal report using only made-up local fixture data."""
    with tempfile.TemporaryDirectory(prefix="mcp-lens-demo-") as temporary:
        demo_home = Path(temporary) / ".codex"
        demo_home.mkdir()
        demo_config = demo_home / "config.toml"
        demo_config.write_text(
            '[mcp_servers.archive-helper]\ncommand = "example-program"\nenabled = true\nenabled_tools = ["list_items"]\n'
            '[plugins."maple@sample"]\nenabled = true\n'
            '[plugins."photos@sample"]\nenabled = true\n', encoding="utf-8")
        for package, slug, app_id in (("maple", "maple_notes", "sample-maple"),
                                      ("photos", "photo_shelf", "sample-photos")):
            manifest = demo_home / f"plugins/cache/sample/{package}/1.0.0/.app.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"apps": {slug: {"id": app_id}}}), encoding="utf-8")
        db = sqlite3.connect(demo_home / "thread_history_1.sqlite")
        db.execute("CREATE TABLE thread_items (created_at_ms INTEGER, item_type TEXT, item_json TEXT, thread_id TEXT)")
        now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        for days_ago, item in (
            (2, {"id": "sample-call-1", "server": "codex_apps", "tool": "maple_notes.search",
                 "appContext": {"connectorId": "sample-maple"}, "status": "completed"}),
            (3, {"id": "sample-call-2", "server": "codex_apps", "tool": "maple_notes.read_page",
                 "appContext": {"connectorId": "sample-maple"}, "status": "completed"}),
            (45, {"id": "sample-call-3", "server": "archive-helper", "tool": "list_items",
                  "status": "completed"}),
        ):
            db.execute("INSERT INTO thread_items VALUES (?, ?, ?, ?)",
                       (now_ms - days_ago * 86400 * 1000, "mcpToolCall", json.dumps(item), "sample-thread"))
        db.commit()
        db.close()
        state = sqlite3.connect(demo_home / "state_1.sqlite")
        state.execute("CREATE TABLE threads (id TEXT, cwd TEXT)")
        state.execute("INSERT INTO threads VALUES (?, ?)", ("sample-thread", "/example/project"))
        state.commit()
        state.close()
        coverage = {}
        _, rows = load_inventory(demo_config, demo_home, coverage, project_dir=Path("/example/project"))
        for row in rows:
            if row.get("kind") == "connector":
                row["name"] = {"sample-maple": "Maple Notes", "sample-photos": "Photo Shelf"}.get(row.get("connector_id"), row["name"])
                row["runtime_app"] = {"enabled": True, "callable": True}
                row["runtime_checked"] = True
                row["runtime_tool_summaries"] = (
                    [{"title": "Search notes", "enabled": True}, {"title": "Read a note", "enabled": True}]
                    if row.get("connector_id") == "sample-maple" else
                    [{"title": "Browse photos", "enabled": True}]
                )
        render_html(rows, Path("/example/config.toml"), output,
                    app_status_note="Example app status, invented for this sample.",
                    history_coverage=coverage,
                    config_note="Example settings and activity, invented for this sample.", demo=True,
                    permission_config={"sandbox_mode": "workspace-write",
                                       "approval_policy": "on-request",
                                       "sandbox_workspace_write": {"network_access": False,
                                                                   "writable_roots": ["/example/project"]}},
                    permissions_effective=True,
                    discovery={"skills_state": "checked", "hooks_state": "checked",
                               "skills": [{"name": "Organise notes", "description": "Help sort project notes.",
                                           "enabled": True, "scope": "repo",
                                           "path": "/example/project/.agents/skills/organise/SKILL.md"}],
                               "hooks": [{"eventName": "preToolUse", "enabled": True,
                                          "handlerType": "command", "source": "project",
                                          "sourcePath": "/example/project/.codex/hooks.json"}]})


def table_path(header: str) -> tuple[str, ...] | None:
    """Parse a TOML table header into decoded key components."""
    try:
        doc = tomllib.loads(header + "\n__mcp_lens_marker = true\n")
    except tomllib.TOMLDecodeError:
        return None

    def find_marker(value: object, path: tuple[str, ...] = ()) -> tuple[str, ...] | None:
        if isinstance(value, dict):
            if value.get("__mcp_lens_marker") is True:
                return path
            for key, child in value.items():
                found = find_marker(child, path + (str(key),))
                if found:
                    return found
        elif isinstance(value, list):
            for index, child in enumerate(value):
                found = find_marker(child, path + (str(index),))
                if found:
                    return found
        return None

    return find_marker(doc)


def section_bounds_path(text: str, target: tuple[str, ...]) -> tuple[int, int] | None:
    """Find a TOML table and any immediately nested tables that belong to it."""
    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if table_path(line.strip()) == target:
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        s = lines[i].strip()
        if s.startswith("["):
            path = table_path(s)
            if path is None or path[:len(target)] != target:
                end = i
                break
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return offsets[start], offsets[end]


def section_bounds(text: str, name: str) -> tuple[int, int] | None:
    return section_bounds_path(text, ("mcp_servers", name))


def set_plugin_server_disabled(text: str, plugin_key: str, server_name: str) -> str:
    # Share the validated table editor with direct MCP and app settings.
    # Appending a table after an inline mcp_servers value would otherwise make
    # config.toml invalid, especially through the CLI remove command.
    return set_section_disabled(text, ("plugins", plugin_key, "mcp_servers", server_name))


def set_section_disabled(text: str, target: tuple[str, ...]) -> str:
    """Set one TOML table's enabled flag without changing unrelated entries."""
    parsed = tomllib.loads(text)
    value: object = parsed
    for part in target:
        value = value.get(part) if isinstance(value, dict) else None
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if table_path(line.strip()) == target), None)
    if start is None and value is not None:
        raise ValueError("This entry uses a settings form MCP Lens cannot edit safely.")
    if start is None:
        header = "[" + ".".join(json.dumps(part, ensure_ascii=False) for part in target) + "]"
        suffix = "" if not text or text.endswith("\n") else "\n"
        updated = text + suffix + "\n" + header + "\nenabled = false\n"
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        enabled_line = re.compile(r"^\s*enabled\s*=")
        match = next((i for i in range(start + 1, end) if enabled_line.match(lines[i])), None)
        if match is None:
            lines.insert(end, "enabled = false\n")
        else:
            lines[match] = "enabled = false\n"
        updated = "".join(lines)
    tomllib.loads(updated)
    return updated


def disable_entry(config_path: Path, row: dict) -> str:
    """Turn off one app or MCP entry in Codex's user config; make no backup."""
    if row.get("plugin_key") == "codex-app-tools@openai-bundled":
        raise ValueError("This is a built-in Codex feature. Manage its permissions in Codex.")
    original = config_path.read_text(encoding="utf-8")
    if row.get("kind") == "connector":
        key = row.get("app_slug")
        if not isinstance(key, str) or not key:
            raise ValueError("This app has no clear Codex settings key, so MCP Lens cannot turn it off safely.")
        apps = tomllib.loads(original).get("apps", {})
        connector_id = row.get("connector_id")
        if (isinstance(apps, dict) and key not in apps and isinstance(connector_id, str)
                and isinstance(apps.get(connector_id), dict)):
            key = connector_id
        updated = set_section_disabled(original, ("apps", key))
        result = "App switched off in Codex settings"
    elif row.get("plugin_key") is None:
        target = ("mcp_servers", str(row["name"]))
        if section_bounds_path(original, target) is None:
            raise ValueError("This MCP server comes from another settings layer. Open that layer in Codex to turn it off.")
        updated = set_section_disabled(original, target)
        result = "MCP server switched off in Codex settings"
    else:
        updated = set_plugin_server_disabled(original, str(row["plugin_key"]), str(row["name"]))
        tomllib.loads(updated)
        result = "Plugin MCP server switched off in Codex settings"
    if updated == original:
        return "Already switched off in Codex settings"
    if config_path.read_text(encoding="utf-8") != original:
        raise ValueError("Codex settings changed during this action. Refresh the report and try again.")
    atomic_write_config(config_path, updated)
    return result


def remove_direct_mcp_entry(config_path: Path, row: dict) -> str:
    """Remove one direct MCP config table without creating a backup."""
    if row.get("kind") == "connector" or row.get("plugin_key") is not None:
        raise ValueError("Only MCP servers listed directly in Codex settings can be removed here.")
    original = config_path.read_text(encoding="utf-8")
    bounds = section_bounds_path(original, ("mcp_servers", str(row["name"])))
    if bounds is None:
        raise ValueError("This MCP server is not in the user settings file. Refresh the report.")
    begin, end = bounds
    updated = original[:begin] + original[end:]
    tomllib.loads(updated)
    if config_path.read_text(encoding="utf-8") != original:
        raise ValueError("Codex settings changed during this action. Refresh the report and try again.")
    atomic_write_config(config_path, updated)
    return "MCP server removed from Codex settings"


def atomic_write_config(path: Path, text: str) -> None:
    """Replace config atomically while preserving its existing mode bits."""
    mode = path.stat().st_mode & 0o777
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".tmp-", delete=False) as temp:
            temp_name = temp.name
            temp.write(text)
            temp.flush()
            os.fsync(temp.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def remove_server(config_path: Path, row: dict) -> None:
    if row.get("kind") == "connector":
        raise SystemExit("App connectors cannot be removed safely from this report; manage the plugin/connection in Codex settings.")
    if row.get("plugin_key") == "codex-app-tools@openai-bundled":
        raise SystemExit("This is a built-in Codex feature. Manage its permissions in Codex.")
    identifier = row["id"]
    print(f"Selected: {identifier!r}")
    if input(f"Type the full ID {identifier!r} to confirm: ").strip() != identifier:
        print("Cancelled; no files changed.")
        return
    text = config_path.read_text(encoding="utf-8")
    if row["plugin_key"] is None:
        bounds = section_bounds_path(text, ("mcp_servers", row["name"]))
        if bounds is None:
            raise SystemExit(f"No removable [mcp_servers.{row['name']}] table found in {config_path}")
        begin, end = bounds
        updated = text[:begin] + text[end:]
        action = "Removed user MCP config entry"
    else:
        try:
            updated = set_plugin_server_disabled(text, row["plugin_key"], row["name"])
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        action = "Disabled plugin MCP through Codex config"
    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"Codex settings cannot be edited safely: {exc}") from exc
    if config_path.read_text(encoding="utf-8") != text:
        raise SystemExit("Codex config changed while preparing cleanup; no changes were written. Review and retry.")
    atomic_write_config(config_path, updated)
    print(f"{action} for {row['name']!r}.")


def refresh_managed_report(codex_home: Path, project_dir: Path,
                           share_path: Path | None = None) -> tuple[bytes, bool]:
    """Return fresh private report bytes, even if the optional summary fails."""
    with tempfile.TemporaryDirectory(prefix="mcp-lens-manager-") as directory:
        staged_report = Path(directory) / "report.html"
        refresh = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--home", str(codex_home),
             "report", "--project", str(project_dir), "--output", str(staged_report)] +
            (["--share-output", str(share_path)] if share_path else []),
            capture_output=True, text=True, timeout=60, check=False)
        if not staged_report.is_file():
            raise ValueError("The report could not refresh. Run the report again.")
        body = staged_report.read_bytes()
    return body, refresh.returncode == 0


def serve_report(codex_home: Path, project_dir: Path, report_path: Path,
                 share_path: Path | None = None, open_browser: bool = True) -> None:
    """Serve trusted report bytes and local controls for Codex settings."""
    try:
        trusted_body, initial_summary_ready = refresh_managed_report(codex_home, project_dir,
                                                                      share_path)
    except (OSError, ValueError, UnicodeDecodeError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f"The local manager could not create a fresh report: {exc}") from exc
    token = secrets.token_urlsafe(32)
    prefix = f"/s/{token}"
    config_path = codex_home / "config.toml"
    change_lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def send_private(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self) -> bool:
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self) -> None:
            if not self.valid_host() or self.path != prefix + "/report":
                self.send_error(404)
                return
            self.send_private(200, trusted_body, "text/html; charset=utf-8")

        def do_POST(self) -> None:
            nonlocal trusted_body
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if (not self.valid_host() or self.path != prefix + "/action"
                    or self.headers.get("Origin") != origin
                    or self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json"):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if length < 1 or length > 4096:
                self.send_error(413)
                return
            try:
                request = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                self.send_error(400)
                return
            identifier = request.get("id") if isinstance(request, dict) else None
            action = request.get("action") if isinstance(request, dict) else None
            if not isinstance(identifier, str) or len(identifier) > 500 or action not in {"disable", "remove"}:
                self.send_error(400)
                return
            with change_lock:
                saved_change = False
                refreshed = False
                try:
                    runtime = codex_runtime_snapshot(codex_home, project_dir)
                    effective = runtime.get("config") if isinstance(runtime.get("config"), dict) else None
                    _, rows = load_inventory(config_path, codex_home, config_override=effective,
                                             project_dir=project_dir)
                    row = next((item for item in rows if item.get("id") == identifier), None)
                    if row is None:
                        raise ValueError("This entry is no longer in the current inventory. Refresh the report.")
                    result = (disable_entry(config_path, row) if action == "disable" else
                              remove_direct_mcp_entry(config_path, row))
                    saved_change = True
                    trusted_body, share_ready = refresh_managed_report(codex_home, project_dir,
                                                                       share_path)
                    refreshed = True
                    try:
                        write_private_html(report_path, trusted_body.decode("utf-8"))
                        report_saved = True
                    except (OSError, UnicodeDecodeError):
                        report_saved = False
                    updated = codex_runtime_snapshot(codex_home, project_dir)
                    if action == "remove":
                        effective_now = updated.get("config") if isinstance(updated.get("config"), dict) else None
                        _, fresh_rows = load_inventory(config_path, codex_home, config_override=effective_now,
                                                       project_dir=project_dir)
                        verified = not any(item.get("id") == identifier for item in fresh_rows)
                    elif row.get("kind") == "connector":
                        effective_now = updated.get("config") if isinstance(updated.get("config"), dict) else None
                        folder_state = app_enabled_for_selected_folder(effective_now, row)
                        current = updated.get("apps", {}).get(row.get("connector_id"))
                        verified = (folder_state is False or
                                    (folder_state is None and isinstance(current, dict)
                                     and current.get("enabled") is False))
                    else:
                        effective_now = updated.get("config") if isinstance(updated.get("config"), dict) else None
                        _, fresh_rows = load_inventory(config_path, codex_home, config_override=effective_now,
                                                       project_dir=project_dir)
                        current = next((item for item in fresh_rows if item.get("id") == identifier), None)
                        verified = bool(current and str(current.get("status", "")).startswith("Disabled"))
                    if verified and action == "remove":
                        message = result + ". The refreshed inventory no longer lists it."
                    elif verified and row.get("kind") == "connector":
                        message = result + (". Codex settings for this folder now list it off." if folder_state is False else
                                            ". Codex's general app list now shows it off; this folder's setting could not be checked.")
                    elif verified:
                        message = result + ". Codex now reports it off."
                    else:
                        message = result + ". The current state could not be confirmed; review the refreshed report."
                    if not share_ready:
                        message += " The counts-only summary could not refresh."
                    if not report_saved:
                        message += " The saved HTML report could not update; this browser view is current."
                    payload = {"saved": True, "refreshed": refreshed, "verified": verified, "message": message}
                    status = 200
                except (OSError, ValueError, KeyError, SystemExit, subprocess.TimeoutExpired) as exc:
                    message = str(exc)
                    if saved_change:
                        message = "The Codex setting changed, but MCP Lens could not refresh or verify the result. " + message
                    payload = {"saved": saved_change, "refreshed": refreshed,
                               "verified": False, "message": message}
                    status = 200 if saved_change else 400
            self.send_private(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except OSError:
        print("Local controls could not start. The saved report will open read-only.", flush=True)
        if open_browser:
            webbrowser.open(report_path.as_uri())
        return
    url = f"http://127.0.0.1:{server.server_port}{prefix}/report"
    print("MCP Lens is running on this computer only. Close this Terminal window to stop managing connections.", flush=True)
    print(f"Open this private report: {url}", flush=True)
    if not initial_summary_ready:
        print("The counts-only summary could not refresh. The private browser view is current.", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def cleanup_candidates(rows: list[dict], cutoff: dt.datetime) -> list[dict]:
    """Offer only user-managed MCP entries with an old dated request."""
    return [row for row in rows
            if row.get("kind") != "connector"
            and row.get("plugin_key") != "codex-app-tools@openai-bundled"
            and row.get("last_used")
            and not str(row.get("status", "")).startswith("Disabled")
            and dt.datetime.strptime(row["last_used"], "%Y-%m-%d %H:%M UTC")
            .replace(tzinfo=dt.timezone.utc) < cutoff]


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect Codex MCP configuration locally; no server connections are made.")
    parser.add_argument("--home", type=Path, default=DEFAULT_CODEX_HOME, help="Codex home directory (default: ~/.codex)")
    parser.add_argument("--config", type=Path, help="Config file (default: <home>/config.toml)")
    sub = parser.add_subparsers(dest="action", required=True)
    report = sub.add_parser("report", help="Create a local HTML inventory report")
    report.add_argument("--output", type=Path, default=Path("mcp-lens-report.html"))
    report.add_argument("--snapshot", type=Path, help="Compare against and update a local hash-only inventory snapshot")
    report.add_argument("--project", type=Path, default=Path.cwd(), help="Folder whose effective Codex settings should be checked")
    report.add_argument("--share-output", type=Path, help="Also create a redacted HTML summary without connection names or paths")
    report.add_argument("--offline", action="store_true", help="Skip the local Codex app-status and effective-settings checks")
    report.add_argument("--no-animation", action="store_true", help="Show the banner without the moving terminal companion")
    manage = sub.add_parser("manage", help="Open the report with local controls to turn off entries")
    manage.add_argument("--report", type=Path, default=Path("mcp-lens-report.html"))
    manage.add_argument("--share-output", type=Path)
    manage.add_argument("--project", type=Path, default=Path.cwd())
    manage.add_argument("--no-open", action="store_true", help="Print the local URL without opening a browser")
    demo = sub.add_parser("demo", help="Create an example HTML report with fictional data")
    demo.add_argument("--output", type=Path, default=Path("mcp-lens-demo.html"))
    clean = sub.add_parser("cleanup", help="Review MCP entries with old matching history; removal requires --apply and confirmation")
    clean.add_argument("--older-than", type=int, default=90, metavar="DAYS")
    clean.add_argument("--apply", action="store_true", help="Offer removal of each review candidate with typed confirmation")
    remove = sub.add_parser("remove", help="Remove/disable one MCP entry with typed confirmation")
    remove.add_argument("id", help="Exact inventory ID, for example global:server or plugin:plugin@source:server")
    args = parser.parse_args()
    config_path = args.config or (args.home / "config.toml")

    if args.action == "manage":
        if args.config is not None:
            raise SystemExit("The local manager uses Codex's normal settings file; --config is not supported.")
        project_dir = args.project.expanduser().resolve()
        report_path = args.report.expanduser().resolve()
        share_path = args.share_output.expanduser().resolve() if args.share_output else None
        if not project_dir.is_dir() or not report_path.is_file() or report_path.suffix.lower() not in {".html", ".htm"}:
            raise SystemExit("Choose an existing project folder and a generated HTML report.")
        if share_path == report_path:
            raise SystemExit("The share summary and private report must have different paths.")
        serve_report(args.home.expanduser().resolve(), project_dir, report_path, share_path,
                     open_browser=not args.no_open)
        return

    if args.action == "demo":
        demo_path = Path(os.path.abspath(args.output.expanduser()))
        if demo_path.suffix.lower() not in {".html", ".htm"}:
            raise SystemExit("Demo report must have an .html or .htm name")
        if demo_path == Path(os.path.abspath(config_path.expanduser())):
            raise SystemExit("Demo report cannot replace the Codex settings file")
        build_demo_report(demo_path)
        print(f"Wrote fictional demo report: {demo_path}")
        return

    if args.action == "remove":
        _, inventory = load_inventory(config_path, args.home)
        selected = next((row for row in inventory if row["id"] == args.id), None)
        if selected is None:
            raise SystemExit(f"No MCP entry with ID {args.id!r} found in the scanned inventory")
        remove_server(config_path, selected)
        return
    history_coverage = {}
    if args.action == "report":
        output_path = Path(os.path.abspath(args.output.expanduser()))
        share_path = Path(os.path.abspath(args.share_output.expanduser())) if args.share_output else None
        snapshot_path = Path(os.path.abspath(args.snapshot.expanduser())) if args.snapshot else None
        if output_path.suffix.lower() not in {".html", ".htm"} or (share_path and share_path.suffix.lower() not in {".html", ".htm"}):
            raise SystemExit("Report files must have an .html or .htm name")
        if share_path == output_path:
            raise SystemExit("--share-output must be different from --output")
        if snapshot_path and snapshot_path.suffix.lower() != ".json":
            raise SystemExit("The change-tracking file must have a .json name")
        destination_paths = [p for p in (output_path, share_path, snapshot_path,
                                           Path(os.path.abspath(config_path.expanduser()))) if p is not None]
        if len(set(destination_paths)) != len(destination_paths):
            raise SystemExit("Report, summary, change-tracking, and settings files must have different paths")
        project_dir = args.project.expanduser().resolve()
        if not project_dir.is_dir():
            raise SystemExit(f"Project folder not found: {project_dir}")
        with TerminalCompanion(animate=not args.no_animation):
            runtime = None
            if args.offline:
                app_status_note = "App enabled status was not checked because offline mode was selected."
                config_note = "Only the user settings file was checked because offline mode was selected."
            elif args.config is not None and config_path.expanduser().resolve() != (args.home / "config.toml").expanduser().resolve():
                app_status_note = "App enabled status was not checked because this report uses a separate settings file."
                config_note = "Only the explicitly selected settings file was checked."
            else:
                runtime = codex_runtime_snapshot(args.home, project_dir)
                app_status_note = runtime["app_note"]
                config_note = runtime["config_note"]
            effective = runtime.get("config") if runtime else None
            inventory_config, rows = load_inventory(config_path, args.home, history_coverage, effective, project_dir)
            annotate_source_locations(rows, config_path, runtime.get("layers") if runtime else None)
            runtime_apps = runtime.get("apps", {}) if runtime else {}
            runtime_summaries = runtime.get("app_summaries", {}) if runtime else {}
            app_settings = inventory_config.get("apps", {}) if isinstance(inventory_config, dict) else {}
            app_settings = app_settings if isinstance(app_settings, dict) else {}
            for row in rows:
                if row.get("kind") == "connector":
                    row["runtime_app"] = runtime_apps.get(row.get("connector_id"))
                    row["runtime_checked"] = app_status_note == APP_STATUS_CHECKED_NOTE
                    row["runtime_tool_summaries"] = runtime_summaries.get(row.get("connector_id"))
                    row["selected_folder_app_enabled"] = app_enabled_for_selected_folder(effective, row)
                    slug = row.get("app_slug")
                    policy = app_settings.get(slug)
                    if not isinstance(policy, dict):
                        policy = app_settings.get(row.get("connector_id"))
                    row["app_policy"] = policy if isinstance(policy, dict) else None
                    row["reference_only"] = bool(
                        row.get("plugin_key") and row["runtime_app"] is None
                        and row["app_policy"] is None and not int(row.get("use_count", 0)))
                    default_policy = app_settings.get("_default")
                    row["app_default_policy"] = default_policy if isinstance(default_policy, dict) else None
            changes = None
            note = None
            if snapshot_path:
                changes, note = snapshot_inventory(snapshot_path, rows)
                if note.startswith(("The saved change-tracking file could not",
                                    "This change-tracking file uses an unsupported format")):
                    changes = None
            file_changes = folder_file_changes(args.home, project_dir)
            render_html(rows, config_path, output_path, changes, note,
                        app_status_note, history_coverage, config_note,
                        project_dir=project_dir, file_changes=file_changes,
                        discovery=runtime, permission_config=inventory_config,
                        permissions_effective=isinstance(effective, dict))
            if share_path:
                render_share_html(rows, share_path, history_coverage)
        if sys.stdout.isatty():
            print_terminal_summary(rows, project_dir, output_path, app_status_note)
        else:
            print(f"Wrote local report: {terminal_text(output_path)}")
        if share_path:
            print(f"Counts-only summary: {terminal_text(share_path)}")
        return
    _, rows = load_inventory(config_path, args.home, history_coverage)
    if args.older_than < 1:
        raise SystemExit("--older-than must be at least one day")
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.older_than)
    candidates = cleanup_candidates(rows, cutoff)
    unknown = [r for r in rows if not r["last_used"]]
    print(f"MCP entries whose last matching request in the saved history checked is {args.older_than}+ days old:")
    for row in candidates:
        print(f"  {row['id']!r}: last observed {row['last_used']}")
    print(f"No matching dated request found: {len(unknown)} (not a cleanup candidate).")
    print("An old date does not prove an entry is unused; Codex history may be incomplete.")
    if not args.apply:
        print("Dry run only. Add --apply to review candidates one by one; global entries are removed and plugin entries are disabled in config.")
        return
    if (history_coverage.get("files_unreadable") or history_coverage.get("files_unsupported")
            or history_coverage.get("records_unreadable") or history_coverage.get("session_files_unreadable")
            or history_coverage.get("session_records_unreadable")
            or int(history_coverage.get("session_calls_missing", 0)) > int(history_coverage.get("session_calls_added", 0))):
        raise SystemExit("Some history could not be read. Automatic cleanup review is unavailable; inspect the entries and use remove with an exact ID if needed.")
    for row in candidates:
        remove_server(config_path, row)


if __name__ == "__main__":
    main()
