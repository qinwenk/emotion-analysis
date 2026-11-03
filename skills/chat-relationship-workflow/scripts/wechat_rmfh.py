#!/usr/bin/env python3
"""Offline RMFH backup inspection and authenticated AES-GCM decryption.

Format research: https://www.52pojie.cn/thread-2068774-1-1.html
This program does not obtain keys, access processes, or contact a network.
It handles RMFH backup containers, not SQLCipher desktop databases.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
import getpass
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

BLOCK = 1024 * 1024
WRAPPER = 128
KEY_ERROR = "认证失败：密钥不匹配、文件发生变化，或此版本使用了不同格式参数。"
EXPECTED_RMFH_NAMES = {"backup.attr", "backup_time.dat", "phoneid.dat", "phone_history.dat", "pkg.attr", "tar_index.dat"}


class ToolError(Exception):
    pass


def crypto_api():
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise ToolError("缺少 cryptography 依赖。请按本 skill 的 requirements.txt 安装。") from exc
    return Cipher, algorithms, modes, InvalidTag


def parse_key(text: str) -> bytes:
    value = re.sub(r"[\s:]", "", text.strip())
    if value.lower().startswith("0x"):
        value = value[2:]
    if len(value) not in (32, 48, 64) or not re.fullmatch(r"[0-9a-fA-F]+", value):
        raise ToolError("密钥必须是 32、48 或 64 个十六进制字符（16、24 或 32 字节）。")
    return bytes.fromhex(value)


def read_key(key_file: str | None) -> bytes:
    if key_file:
        p = Path(key_file).expanduser().resolve(strict=True)
        if p.stat().st_size > 4096:
            raise ToolError("密钥文件过大；只接受保存十六进制密钥的小文本文件。")
        return parse_key(p.read_text(encoding="utf-8-sig"))
    # Do not fall back to echoed input or accept a key in command-line arguments.
    if not sys.stdin.isatty():
        raise ToolError("非交互运行请使用 --key-file；不会从可能回显的输入读取密钥。")
    return parse_key(getpass.getpass("输入备份解密密钥（十六进制，输入不显示）："))


def is_link(path: Path) -> bool:
    s = path.lstat()
    return stat.S_ISLNK(s.st_mode) or bool(getattr(s, "st_file_attributes", 0) & 0x400)


def file_kind(path: Path) -> str:
    with path.open("rb") as f:
        start = f.read(4)
        size = os.fstat(f.fileno()).st_size
        if start != b"RMFH":
            if any(x in path.parts for x in ("ChatPackage", "Index", "Media")) or path.name in EXPECTED_RMFH_NAMES:
                return "expected_rmfh_missing"
            return "plain_or_unknown"
        if size < 256:
            return "invalid_rmfh"
        f.seek(-128, os.SEEK_END)
        return "rmfh" if f.read(4) == b"RMFT" else "invalid_rmfh"


def inventory(source: Path) -> tuple[Path, list[dict], list[str]]:
    source = source.expanduser().resolve(strict=True)
    if not source.is_file() and not source.is_dir():
        raise ToolError("来源必须是文件或文件夹。")
    root = source if source.is_dir() else source.parent
    files, skipped = [], []
    if source.is_file():
        paths = [source]
    else:
        paths = []
        def walk_error(error):
            raise error
        for current, dirs, names in os.walk(source, followlinks=False, onerror=walk_error):
            current = Path(current)
            kept = []
            for name in sorted(dirs):
                p = current / name
                if is_link(p):
                    skipped.append(str(p.relative_to(root)))
                else:
                    kept.append(name)
            dirs[:] = kept
            for name in sorted(names):
                p = current / name
                if is_link(p):
                    skipped.append(str(p.relative_to(root)))
                elif p.is_file():
                    paths.append(p)
    for path in paths:
        files.append({"path": path, "relative": str(path.relative_to(root)),
                      "bytes": path.stat().st_size, "kind": file_kind(path)})
    return root, files, skipped


def describe(files: list[dict], skipped: list[str]) -> dict:
    categories = Counter()
    spans = []
    for item in files:
        parts = Path(item["relative"]).parts
        category = next((x for x in ("ChatPackage", "Index", "Media") if x in parts), "metadata_or_other")
        categories[category] += 1
        if category == "ChatPackage" and re.fullmatch(r"\d{13}-\d{13}", item["path"].name):
            spans.extend(int(x) for x in item["path"].name.split("-"))
    result = {"file_count": len(files), "bytes": sum(x["bytes"] for x in files),
              "kinds": dict(Counter(x["kind"] for x in files)),
              "categories": dict(categories), "skipped_links": skipped,
              "message_content_decrypted": False,
              "note": "文件名时间范围不是消息完整性验证；RMFH 首尾标记正确不代表密文认证通过。"}
    if spans:
        tz = timezone(timedelta(hours=8))
        result["filename_time_span_utc8"] = [datetime.fromtimestamp(x / 1000, tz).isoformat() for x in (min(spans), max(spans))]
    return result


def decrypt_stream(source: Path, key: bytes, destination=None) -> int:
    """Writes only to a private temporary stream; caller publishes after finalize."""
    Cipher, algorithms, modes, InvalidTag = crypto_api()
    with source.open("rb") as f:
        before = os.fstat(f.fileno())
        if before.st_size < 256:
            raise ToolError("RMFH 文件不足 256 字节。")
        head = f.read(128)
        f.seek(-128, os.SEEK_END)
        tail = f.read(128)
        if head[:4] != b"RMFH" or tail[:4] != b"RMFT":
            raise ToolError("RMFH/RMFT 首尾标记无效。")
        dec = Cipher(algorithms.AES(key), modes.GCM(head[19:31], tail[10:26])).decryptor()
        remaining = before.st_size - 256
        size = remaining
        f.seek(128)
        while remaining:
            block = f.read(min(BLOCK, remaining))
            if not block:
                raise ToolError("读取过程中遇到文件截断。")
            remaining -= len(block)
            plain = dec.update(block)
            if destination is not None:
                destination.write(plain)
        try:
            last = dec.finalize()
        except InvalidTag as exc:
            raise ToolError(KEY_ERROR) from exc
        after = os.fstat(f.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ToolError("文件在解密过程中被修改；已放弃本次输出。")
        if destination is not None:
            destination.write(last)
        return size + len(last)


def sample_files(files: list[dict]) -> list[dict]:
    encrypted = sorted((x for x in files if x["kind"] == "rmfh"), key=lambda x: x["bytes"])
    chats = [x for x in encrypted if "ChatPackage" in x["path"].parts]
    selected = []
    for x in chats[:1] + encrypted:
        if x not in selected:
            selected.append(x)
        if len(selected) >= 3:
            break
    return selected


def verify_samples(files: list[dict], key: bytes) -> list[dict]:
    samples = sample_files(files)
    if not samples:
        raise ToolError("没有找到首尾结构有效的 RMFH 文件。")
    crypto_api()
    results = []
    for item in samples:
        try:
            decrypt_stream(item["path"], key)
            results.append({"file": item["relative"], "authenticated": True})
        except (OSError, ToolError) as exc:
            results.append({"file": item["relative"], "authenticated": False, "error": str(exc)})
    return results


def publish_file(source: Path, destination: Path, key: bytes | None) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise ToolError("目标文件已存在；不会覆盖。")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".rmfh-", suffix=".partial", dir=destination.parent, delete=False) as tmp:
            temp_path = Path(tmp.name)
            if key is None:
                count = 0
                with source.open("rb") as inp:
                    while block := inp.read(BLOCK):
                        tmp.write(block)
                        count += len(block)
            else:
                count = decrypt_stream(source, key, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
        # Atomic no-clobber publication. Never use replace() on an existing file.
        if os.name == "nt":
            os.rename(temp_path, destination)
        else:
            os.link(temp_path, destination)
            temp_path.unlink()
        temp_path = None
        return count
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def decrypt_batch(source: Path, output: Path, key: bytes) -> tuple[dict, int]:
    source = source.expanduser().resolve(strict=True)
    output = output.expanduser().resolve()
    # Enforce disjoint input/output trees. This also avoids encrypting a prior output.
    if output == source or output in source.parents or (source.is_dir() and source in output.parents):
        raise ToolError("输出目录必须与备份目录分开，不能是其子目录或父目录。")
    if output.exists():
        raise ToolError("输出目录已经存在。请换一个新目录；脚本不会覆盖旧结果。")
    _, files, skipped = inventory(source)
    checks = verify_samples(files, key)
    if not any(x["authenticated"] for x in checks):
        raise ToolError("样本均未通过认证，未创建输出目录。" + KEY_ERROR)
    output.mkdir(parents=True, exist_ok=False)
    report = {"format": "wechat-rmfh-aes-gcm", "source": str(source),
              "created_at": datetime.now().astimezone().isoformat(),
              "scope": "解密容器文件；未转换为可读聊天或 ChatLab 消息。",
              "sample_authentication": checks, "skipped_links": skipped, "files": []}
    failures = 0
    report_path = output / "report.json"
    def save_report():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    save_report()
    try:
        for index, item in enumerate(files, 1):
            record = {k: item[k] for k in ("relative", "bytes", "kind")}
            try:
                if item["kind"] in ("invalid_rmfh", "expected_rmfh_missing"):
                    raise ToolError("预期的 RMFH 首尾结构缺失或不完整，未尝试解密或复制。")
                encrypted = item["kind"] == "rmfh"
                dest = output / ("decrypted" if encrypted else "unchanged") / item["relative"]
                record["output_bytes"] = publish_file(item["path"], dest, key if encrypted else None)
                record["status"] = "authenticated_decrypted" if encrypted else "copied_without_decryption"
                record["output"] = str(dest.relative_to(output))
            except (OSError, ToolError) as exc:
                failures += 1
                record["status"] = "failed"
                record["error"] = str(exc)
            report["files"].append(record)
            save_report()
            print(f"[{index}/{len(files)}] {record['status']}: {item['relative']}", flush=True)
    except KeyboardInterrupt:
        report["interrupted"] = True
        save_report()
        raise
    report["summary"] = dict(Counter(x["status"] for x in report["files"]))
    report["all_scanned_files_processed_successfully"] = failures == 0 and not skipped
    save_report()
    return report, 0 if failures == 0 and not skipped else 2


def interactive() -> int:
    default = ""
    print("微信 RMFH 备份工具（离线运行，源文件只读）")
    print("本工具需要另行取得备份密钥，不能用微信登录密码替代。")
    raw = input(f"备份路径 [回车使用 {default or '无默认值'}]：").strip().strip('"')
    source = Path(raw or default)
    if not (raw or default):
        raise ToolError("未提供备份路径。")
    _, files, skipped = inventory(source)
    print(json.dumps(describe(files, skipped), ensure_ascii=False, indent=2))
    action = input("选择：1 仅检查（默认） / 2 验证密钥 / 3 解密到新目录：").strip()
    if action in ("", "1"):
        return 0
    if action not in ("2", "3"):
        raise ToolError("无效选项。")
    key = read_key(None)
    if action == "2":
        results = verify_samples(files, key)
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0 if all(x["authenticated"] for x in results) else 2
    default_output = Path.cwd() / ("decrypted-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    raw_output = input(f"新输出目录 [回车使用 {default_output}]：").strip().strip('"')
    output = Path(raw_output) if raw_output else default_output
    report, code = decrypt_batch(source, output, key)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"结果和逐文件报告：{output.resolve()}")
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="微信 RMFH 备份检查 / AES-GCM 认证解密（需要备份密钥）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("interactive", help="交互菜单，密钥输入不回显")
    for name in ("inspect", "check-key", "decrypt"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--source", required=True, help="原始备份目录或单个 RMFH 文件")
        if name != "inspect":
            cmd.add_argument("--key-file", help="包含十六进制密钥的本地文本文件；省略时隐藏输入")
        if name == "decrypt":
            cmd.add_argument("--output", required=True, help="尚不存在的输出目录，不能位于备份目录内")
    args = parser.parse_args(argv)
    try:
        if args.command == "interactive":
            return interactive()
        source = Path(args.source)
        if args.command == "decrypt":
            report, code = decrypt_batch(source, Path(args.output), read_key(args.key_file))
            print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
            return code
        _, files, skipped = inventory(source)
        if args.command == "inspect":
            print(json.dumps(describe(files, skipped), ensure_ascii=False, indent=2))
            return 2 if any(x["kind"] in ("invalid_rmfh", "expected_rmfh_missing") for x in files) or skipped else 0
        results = verify_samples(files, read_key(args.key_file))
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0 if all(x["authenticated"] for x in results) else 2
    except (ToolError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
