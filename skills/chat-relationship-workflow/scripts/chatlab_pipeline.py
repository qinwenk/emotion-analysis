"""Validate, import and audit one private desktop chat using ChatLab CLI.

Only metadata and count aggregates are queried. Message bodies are never queried
with SQL, printed, or included in errors. The CLI is invoked without a shell.
Import is a write operation: callers must already have authorization for the
specified account, contact and range. This module does not itself ask the user.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from typing import Callable

SUPPORTED_CLI_VERSION = "0.37.7"
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
MAX_EXPORT_BYTES = 256 * 1024 * 1024


class ChatLabPipelineError(RuntimeError):
    """A sanitized failure; imported data is not automatically deleted on error."""

    def __init__(self, code: str, stage: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.details = details or {}


def _fail(code, stage, message, **details):
    raise ChatLabPipelineError(code, stage, message, details)


def resolve_clb_command(clb_path: str | os.PathLike | None = None) -> list[str]:
    """Resolve npm's Windows shim to node + its actual JS entry, not cmd.exe."""
    candidate = str(clb_path) if clb_path else shutil.which("clb") or shutil.which("clb.cmd")
    if not candidate:
        _fail("chatlab_not_found", "cli", "ChatLab CLI is not installed or its path was not provided.")
    path = Path(candidate).expanduser()
    if not path.is_file():
        located = shutil.which(candidate)
        if not located:
            _fail("chatlab_not_found", "cli", "ChatLab CLI path is not a file.")
        path = Path(located)
    path = path.absolute()
    suffix = path.suffix.lower()
    if suffix in {".cmd", ".bat", ".ps1"}:
        if path.name.lower() not in {"clb.cmd", "chatlab.cmd", "clb.ps1", "chatlab.ps1"}:
            _fail("unsupported_cli_launcher", "cli", "Use the ChatLab npm launcher or its JavaScript entry.")
        entry = path.parent / "node_modules" / "chatlab-cli" / "bin" / "chatlab.mjs"
        if not entry.is_file():
            _fail("unsupported_cli_launcher", "cli", "Cannot locate the ChatLab JavaScript entry beside the npm launcher.")
        node = path.parent / "node.exe"
        node_path = str(node) if node.is_file() else shutil.which("node")
        if not node_path:
            _fail("node_not_found", "cli", "Node.js is required to run ChatLab without a command shell.")
        return [node_path, str(entry)]
    if suffix in {".mjs", ".js"}:
        node_path = shutil.which("node")
        if not node_path:
            _fail("node_not_found", "cli", "Node.js is not available.")
        return [node_path, str(path)]
    if os.name == "nt" and suffix != ".exe":
        _fail("unsupported_cli_launcher", "cli", "On Windows, provide the npm launcher, JavaScript entry or executable.")
    return [str(path)]


class _Client:
    def __init__(self, clb_path=None, timeout=180, env=None):
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
            _fail("invalid_timeout", "cli", "Command timeout must be between 0 and 3600 seconds.")
        self.command = resolve_clb_command(clb_path)
        self.timeout = timeout
        self.env = os.environ.copy()
        if env:
            self.env.update(env)

    def call(self, args: list[str], stage: str, manifest=False) -> dict:
        kwargs = dict(capture_output=True, text=True, encoding="utf-8", errors="strict",
                      timeout=self.timeout, env=self.env, shell=False)
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            result = subprocess.run(self.command + args, **kwargs)
        except subprocess.TimeoutExpired:
            _fail("chatlab_timeout", stage, "ChatLab command timed out; inspect the stage before retrying.")
        except (OSError, UnicodeError):
            _fail("chatlab_execution_failed", stage, "ChatLab could not be executed or returned invalid UTF-8.")
        if result.returncode != 0:
            _fail("chatlab_command_failed", stage, "ChatLab returned a nonzero exit status.", exit_code=result.returncode)
        if len(result.stdout) > 16 * 1024 * 1024:
            _fail("chatlab_output_too_large", stage, "Unexpectedly large metadata response.")
        try:
            value = json.loads(result.stdout)
        except (ValueError, TypeError):
            _fail("chatlab_invalid_json", stage, "ChatLab did not return one valid JSON response.")
        if not isinstance(value, dict):
            _fail("chatlab_invalid_response", stage, "ChatLab metadata response is not an object.")
        if not manifest and value.get("ok") is not True:
            _fail("chatlab_rejected", stage, "ChatLab reported an unsuccessful operation.")
        return value

    def manifest(self):
        result = self.call(["manifest"], "manifest", manifest=True)
        if result.get("version") != SUPPORTED_CLI_VERSION or result.get("apiVersion") != 1:
            _fail("unsupported_chatlab_version", "manifest", "This importer requires the verified ChatLab CLI 0.37.7 contract.")
        return result

    def sessions(self):
        response = self.call(["sessions", "list", "--format", "json"], "sessions")
        items = response.get("data", {}).get("items")
        if not isinstance(items, list) or response.get("meta", {}).get("hasMore"):
            _fail("invalid_session_list", "sessions", "The complete session list was not returned.")
        if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in items):
            _fail("invalid_session_list", "sessions", "Session list contains unsupported metadata.")
        return items

    def sql(self, session_id, query, stage):
        response = self.call(["sql", query, "--session", session_id, "--limit", "10", "--format", "json"], stage)
        meta_session = response.get("meta", {}).get("session", {}).get("id")
        if meta_session != session_id:
            _fail("session_mismatch", stage, "ChatLab queried a different session.")
        data = response.get("data", {})
        rows = data.get("rows")
        if data.get("truncated") or not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            _fail("invalid_aggregate", stage, "ChatLab did not return one complete aggregate row.")
        return rows[0]


def _identifier(value, stage="input"):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512 or "\x00" in value:
        _fail("invalid_identity", stage, "A stable account identifier is missing or unsupported.")
    return value


def _literal(value):
    # Identifiers remain values, never SQL syntax, even when they contain quotes.
    return "CAST(X'" + value.encode("utf-8").hex() + "' AS TEXT)"


def _session_id(value):
    if not isinstance(value, str) or not SESSION_ID.fullmatch(value):
        _fail("invalid_session_id", "input", "Session ID must contain 1–128 letters, digits, underscores or hyphens.")
    return value


def _identity_query(owner_id, peer_id):
    owner, peer = _literal(owner_id), _literal(peer_id)
    return (
        "SELECT (SELECT COUNT(*) FROM meta) AS meta_rows, "
        f"(SELECT COUNT(*) FROM meta WHERE owner_id={owner} AND platform='weixin' AND type='private') AS matching_meta, "
        f"(SELECT COUNT(*) FROM member WHERE platform_id={owner}) AS owner_members, "
        f"(SELECT COUNT(*) FROM member WHERE platform_id={peer}) AS peer_members, "
        f"(SELECT COUNT(*) FROM member WHERE platform_id NOT IN ({owner},{peer}) AND LOWER(platform_id)!='system') AS foreign_members, "
        "(SELECT COUNT(*) FROM member WHERE LOWER(platform_id)='system') AS system_members"
    )


def _matches_identity(row):
    return (row.get("meta_rows") == 1 and row.get("matching_meta") == 1
            and row.get("owner_members") == 1 and row.get("peer_members") == 1
            and row.get("foreign_members") == 0 and row.get("system_members") in (0, 1))


def _assert_identity(client, session_id, owner_id, peer_id, stage):
    row = client.sql(session_id, _identity_query(owner_id, peer_id), stage)
    if not _matches_identity(row):
        _fail("session_identity_mismatch", stage, "The existing session does not have this owner and this one counterpart.")


def resolve_session_id(self_id, target_id, clb_path=None, timeout=180, env=None, max_candidates=200):
    """Reuse the unique matching private session; otherwise return a stable ID.

    Only participant/owner metadata is read. Ambiguity or an uninspectable
    candidate blocks automatic creation so old sessions are not silently cloned.
    """
    owner, peer = _identifier(self_id), _identifier(target_id)
    if owner == peer or owner.lower() == "system" or peer.lower() == "system":
        _fail("invalid_identity", "input", "Two distinct ordinary participant IDs are required.")
    client = _Client(clb_path, timeout, env)
    client.manifest()
    candidates = [s for s in client.sessions() if s.get("platform") == "weixin" and s.get("type") == "private"]
    if len(candidates) > max_candidates:
        _fail("too_many_sessions", "session_lookup", "Too many candidate sessions; provide an explicit session ID.")
    matches = []
    for session in candidates:
        sid = _session_id(session["id"])
        row = client.sql(sid, _identity_query(owner, peer), "session_lookup")
        if _matches_identity(row):
            matches.append(sid)
    if len(matches) > 1:
        _fail("session_ambiguous", "session_lookup", "Several sessions match this owner and counterpart; select one.", session_ids=matches)
    if matches:
        return matches[0]
    digest = hashlib.sha256(json.dumps([owner, peer], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()[:20]
    return "wechat-desktop-" + digest


def _read_export(path):
    source = Path(path).expanduser().absolute()
    for part in (source, *source.parents):
        try:
            info = part.lstat()
        except OSError:
            _fail("export_not_found", "input", "The export path does not exist or is not readable.")
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            _fail("export_link_rejected", "input", "Export paths containing links or junctions are not supported.")
    try:
        if not source.is_file() or source.stat().st_size > MAX_EXPORT_BYTES:
            _fail("invalid_export_size", "input", "Export must be a regular JSON file no larger than 256 MiB.")
        raw = source.read_bytes()
        value = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        _fail("invalid_export_json", "input", "Cannot read a valid UTF-8 export JSON file.")
    if not isinstance(value, dict):
        _fail("invalid_export", "input", "Export must be an object.")
    meta, members, messages = value.get("meta"), value.get("members"), value.get("messages")
    if not isinstance(meta, dict) or meta.get("platform") != "weixin" or meta.get("type") != "private":
        _fail("invalid_export_scope", "input", "Only a private Weixin chat is supported.")
    owner = _identifier(meta.get("ownerId"))
    if not isinstance(members, list) or not isinstance(messages, list) or not messages:
        _fail("empty_or_invalid_export", "input", "Export must contain two members and at least one message.")
    ids = [_identifier(m.get("platformId")) if isinstance(m, dict) else _identifier(None) for m in members]
    if len(set(ids)) != len(ids):
        _fail("duplicate_members", "input", "Export contains duplicate member IDs.")
    ordinary = [i for i in ids if i.lower() != "system"]
    if len(ordinary) != 2 or owner not in ordinary:
        _fail("invalid_export_scope", "input", "Export owner must be one of exactly two ordinary members.")
    peer = next(i for i in ordinary if i != owner)
    seen, counts, times = set(), Counter(), []
    for message in messages:
        if not isinstance(message, dict):
            _fail("invalid_message", "input", "Export contains a non-object message.")
        mid = message.get("platformMessageId")
        if not isinstance(mid, str) or not mid or mid in seen:
            _fail("unstable_message_ids", "input", "Every source message requires a unique stable nonempty message ID.")
        seen.add(mid)
        sender, ts, kind = message.get("sender"), message.get("timestamp"), message.get("type")
        if sender == "SYSTEM" and kind in (80, 81):
            role = "system"
        elif sender == owner:
            role = "owner"
        elif sender == peer:
            role = "peer"
        else:
            _fail("unknown_sender", "input", "A message is outside the selected two-person scope.")
        if type(ts) is not int or ts <= 0 or (times and ts < times[-1]):
            _fail("invalid_message_time", "input", "Messages must use positive integer seconds in chronological order.")
        counts[role] += 1
        times.append(ts)
    return source, {"owner_id": owner, "peer_id": peer, "source_count": len(messages),
                    "counts": {role: counts[role] for role in ("owner", "peer", "system")},
                    "first_ts": times[0], "last_ts": times[-1],
                    "sha256": hashlib.sha256(raw).hexdigest()}


def _snapshot(client, session_id, owner, peer, stage):
    _assert_identity(client, session_id, owner, peer, stage)
    row = client.sql(session_id,
        "SELECT COUNT(*) AS total, MIN(msg.ts) AS first_ts, MAX(msg.ts) AS last_ts, "
        f"COALESCE(SUM(CASE WHEN m.platform_id={_literal(owner)} THEN 1 ELSE 0 END),0) AS owner_count, "
        f"COALESCE(SUM(CASE WHEN m.platform_id={_literal(peer)} THEN 1 ELSE 0 END),0) AS peer_count, "
        "COALESCE(SUM(CASE WHEN LOWER(m.platform_id)='system' THEN 1 ELSE 0 END),0) AS system_count, "
        "COALESCE(SUM(CASE WHEN m.id IS NULL THEN 1 ELSE 0 END),0) AS orphan_count, "
        "COALESCE(SUM(CASE WHEN LOWER(m.platform_id)='system' AND msg.type NOT IN (80,81) THEN 1 ELSE 0 END),0) AS invalid_system_count "
        "FROM message msg LEFT JOIN member m ON m.id=msg.sender_id", stage)
    for key in ("total", "owner_count", "peer_count", "system_count", "orphan_count", "invalid_system_count"):
        if type(row.get(key)) is not int or row[key] < 0:
            _fail("invalid_aggregate", stage, "Message count metadata is invalid.")
    if row["orphan_count"] or row["invalid_system_count"] or sum(row[r+"_count"] for r in ("owner", "peer", "system")) != row["total"]:
        _fail("invalid_sender_mapping", stage, "Existing messages have unsupported or unresolved sender mappings.")
    if row["total"] and (type(row.get("first_ts")) is not int or type(row.get("last_ts")) is not int):
        _fail("invalid_aggregate", stage, "Message range metadata is invalid.")
    return {"total": row["total"], "first_ts": row.get("first_ts"), "last_ts": row.get("last_ts"),
            "counts": {r: row[r+"_count"] for r in ("owner", "peer", "system")}}


def _empty_snapshot():
    return {"total": 0, "first_ts": None, "last_ts": None, "counts": {"owner": 0, "peer": 0, "system": 0}}


def _result_counts(response, session_id, source_count, dry_run):
    stage = "dry_run" if dry_run else "import"
    data, meta = response.get("data", {}), response.get("meta", {})
    if data.get("success") is not True or data.get("sessionId") != session_id or meta.get("dryRun") is not dry_run:
        _fail("invalid_import_receipt", stage, "Import response has the wrong session, mode or success state.")
    new, duplicates = data.get("newMessageCount"), data.get("duplicateCount")
    if type(new) is not int or type(duplicates) is not int or min(new, duplicates) < 0 or new + duplicates != source_count:
        _fail("import_count_mismatch", stage, "Import counts do not account for every source message.")
    if dry_run and data.get("totalMessageCount") != source_count:
        _fail("import_count_mismatch", stage, "Preview source count differs from the verified export.")
    if data.get("importMode") not in {"created", "incremental"}:
        _fail("unsupported_import_mode", stage, "Unknown ChatLab import mode.")
    diagnostics = data.get("diagnostics")
    if diagnostics is not None:
        if not isinstance(diagnostics, dict):
            _fail("invalid_import_receipt", stage, "Unsupported import diagnostics.")
        expected = {"messagesReceived": source_count, "messagesWritten": new, "duplicateCount": duplicates, "messagesSkipped": 0}
        for field, wanted in expected.items():
            if field in diagnostics and diagnostics[field] != wanted:
                _fail("import_diagnostics_mismatch", stage, "Import diagnostics contain skipped or mismatched message counts.")
        reasons = diagnostics.get("skipReasons", {})
        if not isinstance(reasons, dict) or any(v != 0 for v in reasons.values()):
            _fail("messages_skipped", stage, "Import diagnostics report skipped messages.")
    return {"new": new, "duplicates": duplicates, "mode": data["importMode"]}


def import_verified(export_file, session_id, clb_path=None, timeout=180, callback: Callable | None = None,
                    env=None, expected_owner_id=None, expected_peer_id=None) -> dict:
    """Import one validated export, auditing scope and before/after aggregates.

    Failures during/after import may leave a completed or partial import. No
    rollback deletes user data. Resume by running the same source/session again;
    stable message IDs and a fresh preview determine any remaining additions.
    Per-sender counts are exact for new sessions; incremental deltas are bounded
    by source counts because the CLI preview exposes only a total duplicate count.
    """
    sid = _session_id(session_id)
    source, expected = _read_export(export_file)
    if ((expected_owner_id is not None and expected["owner_id"] != expected_owner_id)
            or (expected_peer_id is not None and expected["peer_id"] != expected_peer_id)):
        _fail("export_identity_mismatch", "input", "Export identity differs from the selected account and contact.")
    def notify(stage, metadata):
        if callback:
            callback(stage, metadata)
    client = _Client(clb_path, timeout, env)
    client.manifest()
    validate = client.call(["validate", str(source), "--json"], "validate")
    validation = validate.get("data", {})
    if validation.get("valid") is not True or validation.get("errors") not in ([], None) or validation.get("warnings") not in ([], None):
        _fail("export_validation_failed", "validate", "Export validation failed or requires warning review.")
    # The verified validator reports numeric counts; fail closed if nonzero.
    for field in ("errorCount", "warningCount"):
        if validation.get(field) != 0:
            _fail("export_validation_failed", "validate", "Export validation contains errors or unresolved warnings.")
    if validation.get("stats", {}).get("messageCount") != expected["source_count"]:
        _fail("export_validation_failed", "validate", "Validator message count differs from the source export.")
    notify("chatlab_validated", {"source_count": expected["source_count"]})
    exists = sid in {s["id"] for s in client.sessions()}
    before = _snapshot(client, sid, expected["owner_id"], expected["peer_id"], "preflight") if exists else _empty_snapshot()
    notify("chatlab_preflight", {"session_id": sid, "before_count": before["total"], "exists": exists})
    import_args = ["import", str(source), "--format", "chatlab", "--session-id", sid]
    preview = _result_counts(client.call(import_args + ["--dry-run", "--json"], "dry_run"), sid, expected["source_count"], True)
    if (preview["mode"] == "incremental") != exists or (not exists and preview["duplicates"]):
        _fail("preview_scope_changed", "dry_run", "Preview does not match the inspected session state.")
    notify("chatlab_previewed", {"session_id": sid, **preview})
    # Detect input or session changes between preview and mutation.
    try:
        unchanged = hashlib.sha256(source.read_bytes()).hexdigest() == expected["sha256"]
    except OSError:
        unchanged = False
    if not unchanged:
        _fail("export_changed", "before_import", "Export changed after validation; rerun from validation.")
    current_exists = sid in {s["id"] for s in client.sessions()}
    if current_exists != exists:
        _fail("session_changed", "before_import", "Session existence changed after preview; rerun preflight.")
    if exists and _snapshot(client, sid, expected["owner_id"], expected["peer_id"], "before_import") != before:
        _fail("session_changed", "before_import", "Session metadata changed after preview; rerun preflight.")
    notify("chatlab_importing", {"session_id": sid, "expected_new_count": preview["new"]})
    receipt = _result_counts(client.call(import_args + ["--json"], "import"), sid, expected["source_count"], False)
    if receipt != preview:
        _fail("preview_import_mismatch", "import", "Actual import counts differ from the preview; verify the session before retrying.")
    after = _snapshot(client, sid, expected["owner_id"], expected["peer_id"], "verify")
    if after["total"] - before["total"] != receipt["new"]:
        _fail("import_verification_failed", "verify", "Database count change does not match the import receipt.")
    deltas = {r: after["counts"][r] - before["counts"][r] for r in expected["counts"]}
    if any(not 0 <= deltas[r] <= expected["counts"][r] for r in deltas) or (not exists and deltas != expected["counts"]):
        _fail("sender_count_mismatch", "verify", "Sender count changes do not match the selected source scope.")
    wanted_first = min(v for v in (before["first_ts"], expected["first_ts"]) if v is not None)
    wanted_last = max(v for v in (before["last_ts"], expected["last_ts"]) if v is not None)
    if after["first_ts"] != wanted_first or after["last_ts"] != wanted_last:
        _fail("range_mismatch", "verify", "Imported database time range differs from the source and prior session.")
    result = {"status": "import_verified", "session_id": sid, "source_count": expected["source_count"],
              "new_count": receipt["new"], "duplicate_count": receipt["duplicates"],
              "before": before, "after": after, "source_counts": expected["counts"], "sender_deltas": deltas,
              "source_range": {"first_ts": expected["first_ts"], "last_ts": expected["last_ts"]},
              "source_sha256": expected["sha256"], "cli_version": SUPPORTED_CLI_VERSION,
              "verification": "owner, participants, total/range, exact new-session sender counts or bounded incremental sender deltas"}
    notify("chatlab_verified", result)
    return result
