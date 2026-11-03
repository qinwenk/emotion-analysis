#!/usr/bin/env python3
"""Bounded, read-only format routing. No messages, keys, network or execution."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import stat
import sys

MAX_JSON_BYTES = 8 * 1024 * 1024


def linked(path: Path) -> bool:
    value = path.lstat()
    return stat.S_ISLNK(value.st_mode) or bool(getattr(value, 'st_file_attributes', 0) & 0x400)


def classify(path: Path) -> str:
    with path.open('rb') as stream:
        head = stream.read(32)
        if head.startswith(b'RMFH'):
            if path.stat().st_size < 256:
                return 'rmfh_damaged'
            stream.seek(-128, os.SEEK_END)
            return 'rmfh_candidate' if stream.read(4) == b'RMFT' else 'rmfh_damaged'
    if head.startswith(b'SQLite format 3\x00'):
        return 'sqlite_plaintext'
    if path.suffix.lower() == '.json' and path.stat().st_size <= MAX_JSON_BYTES:
        try:
            value = json.loads(path.read_text(encoding='utf-8-sig'))
        except (ValueError, UnicodeError):
            return 'json_invalid'
        if (isinstance(value, dict) and isinstance(value.get('messages'), list)
                and isinstance(value.get('members'), list)
                and isinstance(value.get('chatlab'), dict)):
            return 'chatlab_json_candidate'
        return 'json_other'
    if path.name in {'Manifest.plist', 'Manifest.db', 'Info.plist'}:
        return 'apple_backup_metadata_candidate'
    if path.suffix.lower() in {'.db', '.sqlite', '.sqlite3'}:
        return 'database_unknown_or_encrypted'
    if path.suffix.lower() in {'.json', '.jsonl', '.txt', '.csv', '.html'}:
        return 'export_candidate'
    return 'other'


def inspect(source: Path, max_files: int = 1000, max_dirs: int = 2000) -> dict:
    if not 1 <= max_files <= 100000 or not 1 <= max_dirs <= 100000:
        raise ValueError('扫描上限应介于 1 和 100000。')
    source = source.expanduser().absolute()
    # Check before resolve so the user-supplied link itself is not followed.
    if linked(source):
        raise ValueError('来源是链接或重解析点，请提供实际文件或目录。')
    source = source.resolve(strict=True)
    if not (source.is_file() or source.is_dir()):
        raise ValueError('来源必须是普通文件或目录。')
    counts = Counter()
    skipped = 0
    visited_dirs = 0
    inspected = 0
    errors = 0
    limited = False

    def inspect_file(path):
        nonlocal skipped, inspected, errors
        try:
            if linked(path):
                skipped += 1
            elif path.is_file():
                counts[classify(path)] += 1
                inspected += 1
        except (OSError, ValueError):
            errors += 1

    if source.is_file():
        inspect_file(source)
    else:
        def walk_error(_error):
            nonlocal errors
            errors += 1
        for current, dirs, names in os.walk(source, followlinks=False, onerror=walk_error):
            visited_dirs += 1
            if visited_dirs > max_dirs:
                limited = True
                break
            retained = []
            for name in dirs:
                try:
                    if linked(Path(current) / name):
                        skipped += 1
                    else:
                        retained.append(name)
                except OSError:
                    errors += 1
            dirs[:] = retained
            for name in names:
                if inspected + errors >= max_files:
                    limited = True
                    break
                inspect_file(Path(current) / name)
            if limited:
                break
    routes = []
    if counts['rmfh_candidate'] or counts['rmfh_damaged']:
        routes.append('rmfh_backup_requires_authentication_key_then_schema_parser')
    if counts['chatlab_json_candidate']:
        routes.append('clb_validate_then_import_dry_run')
    if counts['sqlite_plaintext'] or counts['database_unknown_or_encrypted']:
        routes.append('identify_database_schema_and_source_before_selecting_adapter')
    if counts['apple_backup_metadata_candidate']:
        routes.append('apple_device_backup_distinct_from_wechat_rmfh')
    if counts['export_candidate'] or counts['json_other']:
        routes.append('identify_export_schema_before_conversion')
    return {
        'source_type': 'file' if source.is_file() else 'directory',
        'files_inspected': inspected, 'signature_counts': dict(counts),
        'scan_complete': not limited and errors == 0 and skipped == 0,
        'limit_reached': limited, 'read_errors': errors, 'links_skipped': skipped,
        'candidate_routes': routes or ['unknown_format'],
        'decrypted': False, 'parsed': False, 'imported': False,
        'note': '格式探测不是解密认证、消息完整性或兼容性证明；不输出聊天内容或密钥。',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--max-files', type=int, default=1000)
    parser.add_argument('--max-dirs', type=int, default=2000)
    args = parser.parse_args()
    try:
        result = inspect(args.source, args.max_files, args.max_dirs)
    except (OSError, ValueError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
