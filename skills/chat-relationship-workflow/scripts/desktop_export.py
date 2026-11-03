#!/usr/bin/env python3
"""Export one private chat from the known Weixin 4.1 Windows WCDB schema.

Requires an existing per-database derived-key JSON file. This script does not
find keys, inspect processes, import into ChatLab, or export media attachments.
Source databases are authenticated by desktop_db_reader, including their WAL.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import xml.etree.ElementTree as ET


MAX_CONTENT_BYTES = 64 * 1024 * 1024
MAX_KEY_FILE_BYTES = 1024 * 1024
REQUIRED_COLUMNS = {
    'local_id', 'sort_seq', 'server_id', 'real_sender_id',
    'create_time', 'local_type', 'message_content',
}
SHARD_NAME = re.compile(r'message_(\d+)\.db\Z')


class ExportError(RuntimeError):
    pass


def is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def reject_link_components(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and is_link(part)):
            raise ExportError('Symbolic links and junctions are not supported for input/output paths.')


def load_keys(path: Path) -> dict:
    reject_link_components(path)
    if not path.is_file() or path.stat().st_size > MAX_KEY_FILE_BYTES:
        raise ExportError('Key file must be a small local JSON file.')
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    except (ValueError, OSError):
        raise ExportError('Cannot read the key-map JSON file.') from None
    mapping = value.get('databases') if isinstance(value, dict) else None
    if not isinstance(mapping, dict):
        raise ExportError('Key map requires a databases object indexed by message/message_N.db.')
    return mapping


def key_for(mapping: dict, relative: str, database: Path) -> bytes:
    entry = mapping.get(relative)
    key = entry.get('key_hex') if isinstance(entry, dict) else None
    if not isinstance(key, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', key):
        raise ExportError(f'Missing or invalid derived 32-byte key for {relative}.')
    if entry.get('salt_hex') is not None:
        salt = entry['salt_hex']
        if not isinstance(salt, str) or not re.fullmatch(r'[0-9a-fA-F]{32}', salt):
            raise ExportError(f'Invalid salt metadata for {relative}.')
        with database.open('rb') as stream:
            actual = stream.read(16)
        if actual != bytes.fromhex(salt):
            raise ExportError(f'The key-map salt does not match {relative}.')
    return bytes.fromhex(key)


def decode_content(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        if len(value.encode('utf-8')) > MAX_CONTENT_BYTES:
            raise ExportError('A message exceeds the supported content size.')
        return value
    if not isinstance(value, bytes) or len(value) > MAX_CONTENT_BYTES:
        raise ExportError('Unsupported message storage type or content size.')
    if value.startswith(b'\x28\xb5\x2f\xfd'):
        try:
            import zstandard
        except ImportError:
            raise ExportError('Compressed messages require the zstandard package.') from None
        try:
            declared_size = zstandard.frame_content_size(value)
            if declared_size not in (zstandard.CONTENTSIZE_UNKNOWN, zstandard.CONTENTSIZE_ERROR) and declared_size > MAX_CONTENT_BYTES:
                raise ExportError('A compressed message declares content larger than the supported limit.')
            # Known-size frames are checked before allocation; unknown-size frames
            # are bounded by max_output_size. Reject truncated or extra frames.
            value = zstandard.ZstdDecompressor().decompress(
                value, max_output_size=MAX_CONTENT_BYTES, allow_extra_data=False)
        except zstandard.ZstdError:
            raise ExportError('Zstandard decompression failed or exceeded the content size limit.') from None
    if len(value) > MAX_CONTENT_BYTES:
        raise ExportError('A decompressed message exceeds the supported content size.')
    try:
        return value.decode('utf-8')
    except UnicodeDecodeError:
        raise ExportError('Message text is not valid UTF-8; no completed export was published.') from None


def classify(raw_type: int, body: str | None) -> tuple[int, str | None]:
    base, sub = raw_type & 0xffffffff, raw_type >> 32
    direct = {1: 0, 3: 1, 34: 2, 43: 3, 47: 5, 48: 8,
              42: 27, 50: 23, 10000: 80, 10002: 81}
    if base != 49:
        kind = direct.get(base, 99)
        if kind == 99 and not body:
            body = f'[未解析消息；微信原始类型 {raw_type}]'
        return kind, body
    app = None
    if body:
        # Do not process DTD/entity declarations from message XML.
        if '<!DOCTYPE' not in body.upper() and '<!ENTITY' not in body.upper():
            try:
                root = ET.fromstring(body)
                app = root if root.tag == 'appmsg' else root.find('.//appmsg')
                if app is not None:
                    value = app.findtext('type')
                    if value and value.isdigit():
                        sub = int(value)
            except (ET.ParseError, ValueError):
                pass
    kind = {3: 24, 4: 24, 5: 7, 6: 4, 19: 26, 33: 24, 36: 24,
            51: 24, 57: 25, 62: 22, 2000: 21, 2001: 20}.get(sub, 99)
    if app is None:
        return kind, body or f'[未解析应用消息；微信原始类型 {raw_type}]'
    title = app.findtext('title') or ''
    if sub == 57:
        ref = app.find('refermsg')
        if ref is not None:
            name = ref.findtext('displayname') or ''
            content = ref.findtext('content') or ''
            return kind, title + '\n[引用' + (' ' + name if name else '') + '] ' + content
        return kind, title or body
    if sub in (3, 4, 5, 6, 19, 33, 36, 51):
        fields = [title, app.findtext('des') or '', app.findtext('url') or '']
        return kind, '\n'.join(x for x in fields if x) or body
    return kind, body


def integer(value, name: str, minimum=0, maximum=(1 << 63) - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ExportError(f'Unsupported value or units in {name}.')
    return value


def quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def raw_value(value):
    if isinstance(value, bytes):
        return {'encoding': 'base64', 'data': base64.b64encode(value).decode('ascii')}
    return value


def read_shard(connection, relative, table_name, self_id, target_id, self_name, target_name):
    """Known-schema adapter; accepts a read-only connection, never guesses identity."""
    connection.row_factory = sqlite3.Row
    names = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    matches = [n for n in names if n.casefold() == table_name.casefold()]
    if not matches:
        return
    if len(matches) != 1:
        raise ExportError('Ambiguous target message table.')
    table = matches[0]
    columns = {r[1] for r in connection.execute('PRAGMA table_info(' + quoted(table) + ')')}
    if not REQUIRED_COLUMNS.issubset(columns):
        raise ExportError(f'Unsupported target schema in {relative}; required message fields are missing.')
    name_tables = [n for n in names if n.casefold() == 'name2id']
    if len(name_tables) != 1:
        raise ExportError(f'Missing or ambiguous Name2Id sender map in {relative}.')
    name_table = quoted(name_tables[0])
    name_columns = {r[1] for r in connection.execute('PRAGMA table_info(' + name_table + ')')}
    if 'user_name' not in name_columns:
        raise ExportError(f'Unsupported sender-map schema in {relative}.')
    try:
        senders = {r[0]: r[1] for r in connection.execute(
            'SELECT rowid,user_name FROM ' + name_table +
            ' WHERE rowid IN (SELECT DISTINCT real_sender_id FROM ' + quoted(table) + ')')}
    except sqlite3.DatabaseError:
        raise ExportError(f'The sender map in {relative} does not expose the required rowid.') from None
    query = 'SELECT * FROM ' + quoted(table) + ' ORDER BY create_time,sort_seq,local_id'
    for record in connection.execute(query):
        row = dict(record)
        timestamp = integer(row['create_time'], 'create_time (Unix seconds)', 946684800, int(time.time()) + 86400)
        local_id = integer(row['local_id'], 'local_id')
        sort_seq = integer(row['sort_seq'], 'sort_seq')
        raw_type = integer(row['local_type'], 'local_type')
        server_id = 0 if row['server_id'] is None else integer(row['server_id'], 'server_id')
        body = decode_content(row['message_content'])
        if body is None and row.get('compress_content') is not None:
            body = decode_content(row['compress_content'])
        kind, content = classify(raw_type, body)
        if kind in (80, 81):
            sender, account_name = 'SYSTEM', '系统'
        else:
            sender_id = integer(row['real_sender_id'], 'real_sender_id')
            sender = senders.get(sender_id)
            if sender not in (self_id, target_id):
                raise ExportError(f'An unresolved or third-party sender occurs in {relative}; private-chat export stopped.')
            account_name = self_name if sender == self_id else target_name
        if server_id:
            stable_id, id_kind = str(server_id), 'wechat_server_id'
        else:
            locator = json.dumps([self_id, target_id, relative, table, local_id, timestamp, sort_seq], ensure_ascii=False, separators=(',', ':'))
            stable_id = 'local-source:' + hashlib.sha256(locator.encode('utf-8')).hexdigest()
            id_kind = 'local_source_locator'
        message = {'sender': sender, 'accountName': account_name, 'timestamp': timestamp,
                   'type': kind, 'content': content, 'platformMessageId': stable_id}
        original = {'database': relative, 'table': table, 'platformMessageId': stable_id,
                    'id_kind': id_kind, 'row': {k: raw_value(v) for k, v in row.items()},
                    'decoded_original_content': body, 'chatlab_type': kind}
        signature = json.dumps([message, raw_type, body], sort_keys=True, ensure_ascii=False)
        yield message, original, signature


def export_chat(db_root: Path, key_file: Path, self_id: str, target_id: str, output: Path,
                self_name='我', target_name='对方', chat_name='聊天记录') -> dict:
    for value in (self_id, target_id):
        if not value or len(value) > 256 or any(ord(c) < 32 for c in value) or value == 'SYSTEM':
            raise ExportError('Provide valid participant IDs, not display names or SYSTEM.')
    if self_id == target_id or self_id.endswith('@chatroom') or target_id.endswith('@chatroom'):
        raise ExportError('This adapter only supports two distinct participants in a private chat.')
    for value in (self_name, target_name, chat_name):
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise ExportError('Display names and chat name must be nonempty and at most 256 characters.')
    db_root, key_file, output = (Path(p).expanduser().absolute() for p in (db_root, key_file, output))
    for path in (db_root, key_file, output):
        reject_link_components(path)
    db_root, key_file, output = (path.resolve() for path in (db_root, key_file, output))
    if not db_root.is_dir() or not output.parent.is_dir():
        raise ExportError('Database root and output parent must already exist as directories.')
    if output.exists():
        raise ExportError('Output must be a new directory; existing results will not be overwritten.')
    if output == db_root or db_root in output.parents or output in db_root.parents:
        raise ExportError('Output must be separate from the source database tree.')
    message_dir = db_root / 'message'
    if not message_dir.is_dir():
        raise ExportError('Expected a db_storage root containing a message directory.')
    reject_link_components(message_dir)
    shards = []
    for path in message_dir.iterdir():
        match = SHARD_NAME.fullmatch(path.name)
        if match:
            reject_link_components(path)
            if not path.is_file():
                raise ExportError('A message shard is not a regular file.')
            shards.append((int(match[1]), path))
    shards.sort()
    if not shards:
        raise ExportError('No supported message_N.db shards were found.')
    keys = load_keys(key_file)
    table_name = 'Msg_' + hashlib.md5(target_id.encode('utf-8')).hexdigest()
    try:
        from desktop_db_reader import read_decrypted_db, DatabaseReadError
    except ImportError:
        raise ExportError('The authenticated reader requires the cryptography package and desktop_db_reader.py.') from None
    messages, seen = [], {}
    counts, shard_reports = Counter(), []
    with tempfile.TemporaryDirectory(prefix='.desktop-export-', dir=output.parent) as staging_name:
        staging = Path(staging_name)
        with (staging / 'raw_messages.jsonl').open('x', encoding='utf-8', newline='\n') as raw_out:
            for _, database in shards:
                relative = database.relative_to(db_root).as_posix()
                key = key_for(keys, relative, database)
                try:
                    connection = read_decrypted_db(database, key)
                except DatabaseReadError:
                    raise ExportError(f'Authentication, snapshot, or database validation failed for {relative}; no completed export was published.') from None
                shard_count = 0
                try:
                    for message, original, signature in read_shard(
                            connection, relative, table_name, self_id, target_id, self_name, target_name):
                        stable_id = message['platformMessageId']
                        duplicate = stable_id in seen
                        if duplicate and seen[stable_id] != signature:
                            raise ExportError('Conflicting records share a message ID; export stopped for review.')
                        original['duplicate'] = duplicate
                        raw_out.write(json.dumps(original, ensure_ascii=False, separators=(',', ':')) + '\n')
                        counts['raw_records'] += 1
                        shard_count += 1
                        if duplicate:
                            counts['identical_duplicates_removed'] += 1
                            continue
                        seen[stable_id] = signature
                        messages.append(message)
                        counts[original['id_kind']] += 1
                except sqlite3.DatabaseError:
                    raise ExportError(f'Unsupported or inconsistent message schema in {relative}.') from None
                finally:
                    connection.close()
                shard_reports.append({'database': relative, 'records': shard_count, 'authenticated': True})
        if not messages:
            raise ExportError('No messages were found for the supplied target ID; confirm identity and source coverage.')
        messages.sort(key=lambda message: message['timestamp'])
        document = {
            'chatlab': {'version': '0.0.2', 'exportedAt': int(time.time()),
                        'generator': 'Authenticated Windows Weixin 4.1 schema adapter',
                        'description': 'Desktop database source, not RMFH backup decryption. Media files are not exported or transcribed. Original fields are in raw_messages.jsonl.'},
            'meta': {'name': chat_name, 'platform': 'weixin', 'type': 'private', 'ownerId': self_id},
            'members': [{'platformId': self_id, 'accountName': self_name},
                        {'platformId': target_id, 'accountName': target_name}],
            'messages': messages,
        }
        type_counts = Counter(message['type'] for message in messages)
        participant_counts = Counter(message['sender'] for message in messages)
        report = {
            'status': 'exported_not_imported', 'source_kind': 'desktop_wcdb_not_rmfh',
            'supported_schema': 'Windows Weixin 4.1 message_N.db / Msg_md5(target_id) / Name2Id',
            'source_scope': 'All discovered message_N.db shards; only the specified private conversation. Each shard has its own authenticated snapshot, not a globally locked instant. This does not prove full phone history.',
            'owner_id': self_id, 'target_id': target_id, 'shards': shard_reports,
            'raw_records': counts['raw_records'], 'exported_messages': len(messages),
            'identical_duplicates_removed': counts['identical_duplicates_removed'],
            'wechat_server_ids': counts['wechat_server_id'],
            'local_source_ids': counts['local_source_locator'],
            'local_source_id_limit': 'Converter-generated stable source locators, not original WeChat IDs. Re-sharding or rebuilding the source may change them; cross-source deduplication is not guaranteed.',
            'message_type_counts': dict(type_counts), 'sender_counts': dict(participant_counts),
            'system_records': participant_counts['SYSTEM'],
            'participant_messages': len(messages) - participant_counts['SYSTEM'],
            'unsupported_type_records_preserved': type_counts[99],
            'unresolved_senders': 0, 'decode_errors': 0,
            'first_timestamp': messages[0]['timestamp'], 'last_timestamp': messages[-1]['timestamp'],
            'timestamp_unit': 'Unix seconds', 'display_timezone': 'UTC',
            'first_time_utc': datetime.fromtimestamp(messages[0]['timestamp'], timezone.utc).isoformat(),
            'last_time_utc': datetime.fromtimestamp(messages[-1]['timestamp'], timezone.utc).isoformat(),
            'attachments_exported': False, 'audio_transcribed': False,
            'chatlab_import_performed': False,
        }
        (staging / 'chatlab.json').write_text(json.dumps(document, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        (staging / 'audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        # Exclusively create the result directory, then publish only new files.
        # If publication fails, remove only files created by this invocation.
        output.mkdir(exist_ok=False)
        published = []
        try:
            for name in ('raw_messages.jsonl', 'chatlab.json', 'audit.json'):
                destination = output / name
                with destination.open('xb') as out:
                    published.append(destination)
                    with (staging / name).open('rb') as source:
                        shutil.copyfileobj(source, out)
        except BaseException:
            for path in published:
                if path.is_file() and not is_link(path):
                    path.unlink()
            try:
                output.rmdir()
            except OSError:
                pass
            raise
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db-root', type=Path, required=True)
    parser.add_argument('--key-file', type=Path, required=True, help='Local JSON: databases["message/message_N.db"].key_hex (64 hex characters).')
    parser.add_argument('--self-id', required=True)
    parser.add_argument('--target-id', required=True)
    parser.add_argument('--output', type=Path, required=True, help='New result directory outside the source tree.')
    parser.add_argument('--self-name', default='我')
    parser.add_argument('--target-name', default='对方')
    parser.add_argument('--chat-name', default='聊天记录')
    args = parser.parse_args()
    try:
        result = export_chat(args.db_root, args.key_file, args.self_id, args.target_id,
                             args.output, args.self_name, args.target_name, args.chat_name)
    except (ExportError, OSError, ValueError) as exc:
        # Expected errors contain only controlled diagnostics, never key/content data.
        text = str(exc) if isinstance(exc, ExportError) else 'File access or input validation failed; no success is claimed.'
        print(json.dumps({'status': 'failed', 'error': text}, ensure_ascii=False))
        return 2
    print(json.dumps({key: result[key] for key in ('status', 'exported_messages', 'system_records', 'local_source_ids', 'unsupported_type_records_preserved', 'chatlab_import_performed')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
