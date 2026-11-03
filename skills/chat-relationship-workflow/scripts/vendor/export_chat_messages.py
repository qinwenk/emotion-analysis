#!/usr/bin/env python3
"""Export decoded ChatPackage protobuf records without requiring a schema."""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path


class DecodeError(ValueError):
    pass


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise DecodeError('截断的 varint')
        byte = data[pos]
        pos += 1
        if shift == 63 and byte > 1:
            raise DecodeError('varint 超出 64 位')
        result |= (byte & 0x7f) << shift
        if byte < 0x80:
            return result, pos
    raise DecodeError('varint 超出 64 位')


def fields(data: bytes):
    pos = 0
    while pos < len(data):
        tag, pos = read_varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if not 1 <= number <= 0x1fffffff:
            raise DecodeError('无效的 protobuf 字段号')
        if wire == 0:
            value, pos = read_varint(data, pos)
        elif wire in (1, 2, 5):
            if wire == 2:
                length, pos = read_varint(data, pos)
            else:
                length = 8 if wire == 1 else 4
            if length > len(data) - pos:
                raise DecodeError('字段长度超出消息边界')
            value, pos = data[pos:pos + length], pos + length
        else:
            raise DecodeError(f'不支持的 protobuf wire type：{wire}')
        yield number, wire, value


def encoded(value):
    if isinstance(value, int):
        # JSON numbers are retained as decimal strings to avoid precision loss.
        return {'uint64': str(value)}
    return {'base64': base64.b64encode(value).decode('ascii')}


def one(items, number, wire):
    found = [value for n, w, value in items if n == number and w == wire]
    return found[0] if len(found) == 1 else None


def decode_text(value):
    if value is None:
        return None
    try:
        return value.decode('utf-8')
    except UnicodeDecodeError:
        return None


def timestamp(value):
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec='milliseconds')
    except (ValueError, OverflowError, OSError):
        return None


def exclusive_text(path):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    return os.fdopen(fd, 'w', encoding='utf-8', newline='\n')


def export(source: Path, destination: Path):
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_dir():
        raise ValueError('输入路径必须是已解密备份目录。')
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError('输出目录不能与输入目录相同或互相嵌套。')
    paths = sorted(p for p in source.rglob('*') if p.is_file() and not p.is_symlink() and p.parent.name == 'ChatPackage')
    if not paths:
        raise ValueError('未找到 ChatPackage 文件。')
    destination.mkdir(mode=0o700)
    counts = Counter()
    try:
        with exclusive_text(destination / 'messages.jsonl') as output, exclusive_text(destination / 'messages.html') as view, exclusive_text(destination / 'envelopes.jsonl') as envelopes:
            view.write('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>微信备份消息</title><style>body{max-width:1000px;margin:2em auto;padding:0 1em;font-family:system-ui}article{border-top:1px solid #ccc;padding:1em 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}.meta{color:#555;font-size:.9em}</style><h1>微信备份消息</h1><p>时间为 UTC；类型和字段按备份原始数据列出。媒体消息的正文可能为 XML 或其他引用。</p>')
            for path in paths:
                relative = str(path.relative_to(source))
                top = list(fields(path.read_bytes()))
                groups = [value for n, w, value in top if n == 1 and w == 2]
                if not groups:
                    raise DecodeError(f'{relative}: 未找到消息容器字段 1')
                envelope = [{'number': n, 'wire': w, **encoded(value)} for n, w, value in top if not (n == 1 and w == 2)]
                group_extras = []
                record_count = 0
                for group in groups:
                    for number, wire, raw in fields(group):
                        if number != 1 or wire != 2:
                            group_extras.append({'number': number, 'wire': wire, **encoded(raw)})
                            continue
                        message = list(fields(raw))
                        typ = one(message, 1, 0)
                        party2 = decode_text(one(message, 2, 2))
                        party3 = decode_text(one(message, 3, 2))
                        content = decode_text(one(message, 4, 2))
                        ms = one(message, 5, 0)
                        if typ is None or not any(n == 4 and w == 2 for n, w, _ in message):
                            raise DecodeError(f'{relative}: 消息缺少类型字段或内容字段')
                        item = {'source': relative, 'record_index': record_count, 'type': typ,
                                'field_2': party2, 'field_3': party3, 'content': content,
                                'timestamp_ms': str(ms) if ms is not None else None, 'time_utc': timestamp(ms),
                                'fields': [{'number': n, 'wire': w, **encoded(v)} for n, w, v in message],
                                'raw_message_base64': base64.b64encode(raw).decode('ascii')}
                        output.write(json.dumps(item, ensure_ascii=False, separators=(',', ':')) + '\n')
                        meta = f"{item['time_utc'] or '时间未知'} | 类型 {typ} | 字段 2: {party2 or ''} | 字段 3: {party3 or ''}"
                        display = content if content is not None else '[字段 4 并非有效 UTF-8，请查看 JSONL 中原始字段]'
                        view.write('<article><div class="meta">' + html.escape(meta) + '</div><pre>' + html.escape(display) + '</pre></article>\n')
                        counts[typ] += 1
                        record_count += 1
                envelopes.write(json.dumps({'source': relative, 'record_count': record_count, 'other_top_fields': envelope, 'other_group_fields': group_extras}, ensure_ascii=False, separators=(',', ':')) + '\n')
            view.write('</html>\n')
    except Exception:
        import shutil
        shutil.rmtree(destination)
        raise
    return len(paths), counts


def main():
    parser = argparse.ArgumentParser(description='导出已解密微信 ChatPackage 的消息。')
    parser.add_argument('source', type=Path, help='已解密的备份目录')
    parser.add_argument('--output', '-o', type=Path, required=True, help='新建的输出目录')
    args = parser.parse_args()
    try:
        files, counts = export(args.source, args.output)
    except (OSError, ValueError) as exc:
        parser.exit(1, f'导出失败：{exc}\n')
    print(f'已导出 {files} 个聊天包，共 {sum(counts.values())} 条消息；类型计数：{dict(sorted(counts.items()))}')
    print(f'输出目录：{args.output.resolve()}')


if __name__ == '__main__':
    main()
