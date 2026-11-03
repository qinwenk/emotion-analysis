#!/usr/bin/env python3
"""Windows desktop chat acquisition and verified ChatLab import.

No backup-package decryption, phone control or unattended model invocation.
Keys remain in memory; an agent performs the evidence-based analysis after
``analysis_ready`` and explicitly records its report with ``finish-analysis``.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

SCHEMA = 1
STATE_FILE = 'workflow.json'
EXPORT_FILES = ('chatlab.json', 'raw_messages.jsonl', 'audit.json')
DEFAULT_QUESTION = '分析双方的情感意愿、相处适配、关系关键节点和当前卡点，并给出有证据的下一步。'
SESSION_RE = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')


class WorkflowError(RuntimeError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details or {}


def now():
    return datetime.now(timezone.utc).isoformat()


def checked_path(value):
    if str(value).startswith(('\\\\', '//')):
        raise WorkflowError('remote_path', '本流程只处理本机路径，不使用网络共享目录。')
    path = Path(value).expanduser().absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400):
            raise WorkflowError('linked_path', '路径中包含链接或目录联接，请使用真实目录。')
    return path.resolve()


def separate_paths(source, output):
    if source == output or source in output.parents or output in source.parents:
        raise WorkflowError('source_output_overlap', '任务目录必须与微信数据库目录分开。')


def read_json(path):
    path = checked_path(path)
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise WorkflowError('invalid_state', '任务状态文件缺失或大小异常。')
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (ValueError, OSError):
        raise WorkflowError('invalid_state', '任务状态文件无法读取。') from None


def write_json(path, value):
    checked_path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def safe_metadata(value):
    """Do not persist key material or chat bodies from any stage callback."""
    if isinstance(value, dict):
        return {k: safe_metadata(v) for k, v in value.items()
                if not any(word in k.lower() for word in ('key', 'password', 'secret', 'content', 'stdout', 'stderr'))}
    if isinstance(value, (list, tuple)):
        return [safe_metadata(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def record(state, directory, stage, metadata=None):
    state['stage'] = stage
    state['updated_at'] = now()
    state.setdefault('history', []).append({'stage': stage, 'at': state['updated_at'],
                                            'metadata': safe_metadata(metadata or {})})
    state['history'] = state['history'][-100:]
    write_json(directory / STATE_FILE, state)


@contextmanager
def run_lock(directory):
    path = checked_path(directory / '.workflow.lock')
    with path.open('a+b') as lock:
        if lock.seek(0, 2) == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise WorkflowError('run_busy', '这个任务仍在运行，请勿同时启动第二次。') from None
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def digest(path):
    path = checked_path(path)
    if not path.is_file():
        raise WorkflowError('artifact_missing', '任务文件缺失，请勿沿用不完整的导出。')
    with path.open('rb') as stream:
        value = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
        return value.hexdigest()


def load_state(directory):
    state = read_json(directory / STATE_FILE)
    if not isinstance(state, dict) or state.get('schema_version') != SCHEMA or not isinstance(state.get('request'), dict):
        raise WorkflowError('invalid_state', '无法识别此任务状态。')
    validate_request(state['request'])
    return state


def validate_request(request):
    allowed = {'contact', 'db_root', 'self_id', 'pid', 'since', 'until', 'utc_offset',
               'session_id', 'clb_path', 'scan_timeout', 'question'}
    if set(request) - allowed:
        raise WorkflowError('invalid_request', '请求包含不支持的字段。')
    contact = request.get('contact')
    if not isinstance(contact, str) or not contact.strip() or len(contact) > 256:
        raise WorkflowError('invalid_contact', '请提供一个联系人的备注、昵称或完整账号。')
    if request.get('session_id') is not None and not SESSION_RE.fullmatch(request['session_id']):
        raise WorkflowError('invalid_session', 'ChatLab 会话 ID 格式不正确。')
    for name in ('since', 'until'):
        value = request.get(name)
        if value is not None and (type(value) is not int or not 946684800 <= value <= 253402300799):
            raise WorkflowError('invalid_time_range', '时间范围须为有效日期。')
    if request.get('since') is not None and request.get('until') is not None and request['since'] > request['until']:
        raise WorkflowError('invalid_time_range', '开始日期不能晚于结束日期。')
    if type(request.get('scan_timeout', 120)) is not int or not 5 <= request.get('scan_timeout', 120) <= 300:
        raise WorkflowError('invalid_timeout', '取钥时限须在 5 至 300 秒之间。')
    if request.get('pid') is not None and (type(request['pid']) is not int or request['pid'] <= 0):
        raise WorkflowError('invalid_process', '进程编号无效。')
    question = request.get('question', DEFAULT_QUESTION)
    if not isinstance(question, str) or not question.strip() or len(question) > 4000:
        raise WorkflowError('invalid_question', '请用简短文字说明要分析的问题。')


def discover():
    import desktop_runtime
    return {'status': 'discovered', 'accounts': [str(p) for p in desktop_runtime.discover_accounts()],
            'processes': desktop_runtime.discover_processes(),
            'source': 'Windows desktop databases', 'phone_history_complete': False,
            'sync_note': '先在微信中完成所需聊天同步。官方 Backup 文件夹不是本流程的数据来源；准确数量须解密后核对。'}


def select_source(request):
    import desktop_runtime
    if request.get('db_root'):
        source = checked_path(request['db_root'])
    else:
        accounts = desktop_runtime.discover_accounts()
        if not accounts:
            raise WorkflowError('needs_sync_or_path', '未找到电脑微信数据库。请登录并同步，或指定自定义 db_storage 路径。')
        if len(accounts) != 1:
            raise WorkflowError('needs_account_selection', '发现多个账号目录，请明确选择本次账号。',
                                {'accounts': [str(p) for p in accounts]})
        source = checked_path(accounts[0])
    if not (source / 'contact' / 'contact.db').is_file() or not (source / 'message').is_dir():
        raise WorkflowError('invalid_source', '请选择包含 contact 和 message 的 db_storage 目录，不是 Backup 文件夹。')
    process = None
    if request.get('pid'):
        matches = [p for p in desktop_runtime.discover_processes() if p['pid'] == request['pid']]
        if len(matches) != 1:
            raise WorkflowError('needs_login', '指定的微信进程未找到，请检查登录状态。')
        process = matches[0]
    return source, process


def frozen_export(state, directory):
    name = state.get('export_directory', '')
    if not re.fullmatch(r'export-[1-9][0-9]*', name):
        raise WorkflowError('invalid_state', '导出目录记录无效。')
    export_dir = checked_path(directory / name)
    hashes = state.get('artifact_hashes', {})
    if set(hashes) != set(EXPORT_FILES):
        raise WorkflowError('invalid_state', '导出校验信息不完整。')
    for name in EXPORT_FILES:
        if digest(export_dir / name) != hashes[name]:
            raise WorkflowError('artifact_changed', '已导出的文件被修改，请新建任务重新获取，避免导入不同内容。')
    return export_dir


def execute(state, directory):
    import desktop_runtime
    import desktop_export
    import chatlab_pipeline

    request = state['request']
    if state.get('status') == 'complete':
        return state
    state['status'] = 'running'
    state.pop('error', None)
    record(state, directory, 'resuming' if state.get('history') else 'preflight')
    try:
        if state.get('export_directory'):
            export_dir = frozen_export(state, directory)
        else:
            source, process = select_source(request)
            separate_paths(source, directory)
            request['db_root'] = str(source)
            state['source'] = {'kind': 'windows_desktop_database', 'db_root': str(source),
                               'phone_history_complete': False, 'media_files_exported': False}
            record(state, directory, 'source_selected', state['source'])
            key_mapping = desktop_runtime.acquire_keys(source, process=process,
                                                        timeout=request.get('scan_timeout', 120))
            try:
                record(state, directory, 'keys_verified', {'database_count': len(key_mapping.get('databases', {}))})
                identity = desktop_export.resolve_contact(source, key_mapping, request['contact'],
                                                          self_id=request.get('self_id'))
                state['identity'] = identity
                record(state, directory, 'target_resolved', identity)
                attempt = 1
                while (directory / f'export-{attempt}').exists():
                    attempt += 1
                export_dir = directory / f'export-{attempt}'
                audit = desktop_export.export_chat(
                    source, self_id=identity['self_id'], target_id=identity['target_id'], output=export_dir,
                    self_name='我', target_name=identity['target_name'],
                    chat_name='与' + identity['target_name'] + '的聊天', key_mapping=key_mapping,
                    since=request.get('since'), until=request.get('until'))
            finally:
                key_mapping.clear()
                del key_mapping
            state['export_directory'] = export_dir.name
            state['artifact_hashes'] = {name: digest(export_dir / name) for name in EXPORT_FILES}
            state['coverage'] = audit.get('coverage', {})
            record(state, directory, 'exported', {'coverage': state['coverage'],
                                                 'exported_messages': audit.get('exported_messages')})

        identity = state['identity']
        session_id = state.get('session_id') or request.get('session_id')
        if session_id is None:
            session_id = chatlab_pipeline.resolve_session_id(identity['self_id'], identity['target_id'],
                                                             clb_path=request.get('clb_path'))
        if not isinstance(session_id, str) or not SESSION_RE.fullmatch(session_id):
            raise WorkflowError('invalid_session', '无法确定有效的 ChatLab 会话 ID。')
        state['session_id'] = session_id
        record(state, directory, 'session_selected', {'session_id': session_id})
        result = chatlab_pipeline.import_verified(
            export_dir / 'chatlab.json', session_id, clb_path=request.get('clb_path'),
            expected_owner_id=identity['self_id'], expected_peer_id=identity['target_id'],
            callback=lambda stage, metadata: record(state, directory, stage, metadata))
        state['import_result'] = safe_metadata(result)
        if result.get('status') != 'import_verified':
            raise WorkflowError('import_not_verified', '导入结果未通过验收，不能进入分析。')
        handoff = {'status': 'analysis_ready', 'session_id': session_id, 'identity': identity,
                   'source': state['source'], 'coverage': state['coverage'], 'question': request.get('question', DEFAULT_QUESTION),
                   'report_path': str(directory / 'relationship-report.txt'),
                   'evidence_ledger_path': str(directory / 'evidence-ledger.txt'),
                   'instructions': [
                       '使用本 skill 的关系分析和关键场景参考，正文仅通过 clb --format agent 检索，保留隐私预处理。',
                       '先核对会话身份，按时间和主题取证，记录真实阅读范围、媒体缺口及原文编号。',
                       '区分亲近、恋爱意愿与适配；先给直接判断，按关键场景解释卡点与可执行下一步。',
                       '用户纠正感受后，更新相应事件以及依赖它的模式、建议和本次分析文件。',
                       '实际生成报告和证据台账后，运行 finish-analysis 记录完成；导入成功不等于分析完成。']}
        write_json(directory / 'analysis-request.json', handoff)
        state['status'] = 'analysis_ready'
        record(state, directory, 'analysis_ready', {'handoff': 'analysis-request.json'})
        return state
    except (WorkflowError, desktop_runtime.DesktopRuntimeError, desktop_export.ExportError,
            chatlab_pipeline.ChatLabPipelineError) as exc:
        state['status'] = 'needs_attention'
        state['error'] = {'code': getattr(exc, 'code', 'stage_failed'), 'message': str(exc),
                          'details': safe_metadata(getattr(exc, 'details', {}))}
        record(state, directory, 'stopped', state['error'])
        raise
    except Exception as exc:
        state['status'] = 'needs_attention'
        state['error'] = {'code': 'unexpected_failure', 'message': '执行未完成，请查看阶段记录；未输出异常中的原始数据。',
                          'exception_type': type(exc).__name__}
        record(state, directory, 'stopped', state['error'])
        raise WorkflowError('unexpected_failure', state['error']['message']) from None


def preflight_output(request, directory, frozen=False):
    """Reject source overlap before creating a directory, lock or state file."""
    import desktop_runtime
    if request.get('db_root'):
        sources = [checked_path(request['db_root'])]
    elif frozen:
        raise WorkflowError('invalid_state', '已导出任务缺少原始账号路径。')
    else:
        sources = [checked_path(p) for p in desktop_runtime.discover_accounts()]
    for source in sources:
        separate_paths(source, directory)


def run(request, directory):
    validate_request(request)
    directory = checked_path(directory)
    preflight_output(request, directory)
    if directory.exists():
        raise WorkflowError('run_exists', '任务目录已存在。恢复原任务请用 resume，获取新记录请使用新目录。')
    directory.mkdir(parents=True)
    state = {'schema_version': SCHEMA, 'status': 'created', 'created_at': now(),
             'request': dict(request), 'history': []}
    write_json(directory / STATE_FILE, state)
    with run_lock(directory):
        return execute(state, directory)


def resume(directory, overrides=None):
    directory = checked_path(directory)
    if not directory.is_dir():
        raise WorkflowError('run_not_found', '任务目录不存在。')
    preliminary = load_state(directory)
    preliminary_request = preliminary['request'] | {k: v for k, v in (overrides or {}).items() if v is not None}
    preflight_output(preliminary_request, directory, frozen=bool(preliminary.get('export_directory')))
    with run_lock(directory):
        state = load_state(directory)
        overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
        allowed = {'db_root', 'self_id', 'pid', 'clb_path', 'session_id', 'contact'}
        if set(overrides) - allowed:
            raise WorkflowError('invalid_override', '恢复操作不支持修改这个参数。')
        if state.get('export_directory') and any(k in overrides for k in ('db_root', 'self_id', 'pid', 'contact')):
            raise WorkflowError('frozen_scope', '已有导出的任务不能更换账号或联系人，请新建任务。')
        if state.get('status') == 'complete' and overrides:
            raise WorkflowError('completed_run', '已完成任务不能改参数，请新建任务。')
        state['request'].update(overrides)
        if 'session_id' in overrides:
            state['session_id'] = overrides['session_id']
        validate_request(state['request'])
        return execute(state, directory)


def finish_analysis(directory, report, ledger):
    directory = checked_path(directory)
    if not directory.is_dir():
        raise WorkflowError('run_not_found', '任务目录不存在。')
    with run_lock(directory):
        state = load_state(directory)
        if state.get('status') != 'analysis_ready':
            raise WorkflowError('analysis_not_ready', '只有导入已验收、待分析的任务才能记录分析完成。')
        artifacts = {}
        for kind, value in (('report', report), ('evidence_ledger', ledger)):
            path = checked_path(value)
            if directory not in path.parents or not path.is_file() or path.stat().st_size == 0:
                raise WorkflowError('invalid_analysis_artifact', '报告和证据台账须为本次任务目录内的非空文件。')
            if path.suffix.lower() not in ('.txt', '.md') or path.name in EXPORT_FILES:
                raise WorkflowError('invalid_analysis_artifact', '请提供实际的文字分析报告和证据台账。')
            artifacts[kind] = {'path': str(path.relative_to(directory)), 'sha256': digest(path)}
        if artifacts['report']['path'] == artifacts['evidence_ledger']['path']:
            raise WorkflowError('invalid_analysis_artifact', '报告与证据台账不能指向同一文件。')
        state['analysis_artifacts'] = artifacts
        state['status'] = 'complete'
        record(state, directory, 'analyzed', artifacts)
        return state


def time_boundary(text, offset='+08:00', end=False):
    if text is None:
        return None
    match = re.fullmatch(r'([+-])(\d{2}):(\d{2})', offset)
    if not match or int(match[2]) > 14 or int(match[3]) > 59 or (int(match[2]) == 14 and int(match[3])):
        raise WorkflowError('invalid_timezone', '时区偏移应类似 +08:00。')
    minutes = (int(match[2]) * 60 + int(match[3])) * (1 if match[1] == '+' else -1)
    try:
        value = datetime.fromisoformat(text)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone(timedelta(minutes=minutes)))
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text) and end:
            value += timedelta(days=1, seconds=-1)
        return int(value.timestamp())
    except (ValueError, OverflowError, OSError):
        raise WorkflowError('invalid_time_range', '日期应类似 2026-01-01 或带时区的 ISO 时间。') from None


def summary(state, directory):
    return {k: state[k] for k in ('status', 'stage', 'session_id', 'identity', 'coverage', 'error') if k in state} | {
        'run_dir': str(directory), 'state_file': str(Path(directory) / STATE_FILE),
        'analysis_completed': state.get('status') == 'complete'}


def main(argv=None):
    parser = argparse.ArgumentParser(description='电脑微信目标会话获取、验收导入与分析交接。需 Python 3.10+。')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('discover', help='只检查账号目录与微信进程，不读取密钥或聊天')
    run_parser = sub.add_parser('run', help='获取一个目标会话并验收导入；随后由 agent 完成分析')
    run_parser.add_argument('--contact', required=True)
    run_parser.add_argument('--run-dir', required=True)
    run_parser.add_argument('--db-root')
    run_parser.add_argument('--self-id')
    run_parser.add_argument('--pid', type=int)
    run_parser.add_argument('--since')
    run_parser.add_argument('--until')
    run_parser.add_argument('--utc-offset', default='+08:00')
    run_parser.add_argument('--session-id')
    run_parser.add_argument('--clb', dest='clb_path')
    run_parser.add_argument('--scan-timeout', type=int, default=120)
    run_parser.add_argument('--question', default=DEFAULT_QUESTION)
    resume_parser = sub.add_parser('resume', help='恢复任务，复用已校验导出，不重复获取或伪报分析完成')
    resume_parser.add_argument('--run-dir', required=True)
    for name in ('db-root', 'self-id', 'contact', 'session-id'):
        resume_parser.add_argument('--' + name)
    resume_parser.add_argument('--pid', type=int)
    resume_parser.add_argument('--clb', dest='clb_path')
    status_parser = sub.add_parser('status')
    status_parser.add_argument('--run-dir', required=True)
    finish = sub.add_parser('finish-analysis', help='仅在实际写出报告和证据台账后标记完成')
    finish.add_argument('--run-dir', required=True)
    finish.add_argument('--report', required=True)
    finish.add_argument('--ledger', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'discover':
            output = discover()
        elif args.command == 'run':
            request = vars(args).copy()
            for field in ('command', 'run_dir'):
                request.pop(field)
            request['since'] = time_boundary(args.since, args.utc_offset)
            request['until'] = time_boundary(args.until, args.utc_offset, end=True)
            output = summary(run(request, args.run_dir), args.run_dir)
        elif args.command == 'resume':
            overrides = {k: v for k, v in vars(args).items() if k not in ('command', 'run_dir')}
            output = summary(resume(args.run_dir, overrides), args.run_dir)
        elif args.command == 'status':
            output = summary(load_state(checked_path(args.run_dir)), args.run_dir)
        else:
            output = summary(finish_analysis(args.run_dir, args.report, args.ledger), args.run_dir)
        print(json.dumps({'ok': True, 'data': output}, ensure_ascii=False))
        return 0
    except Exception as exc:
        code = getattr(exc, 'code', 'execution_failed')
        message = str(exc) if hasattr(exc, 'code') else '执行未完成，请检查依赖和任务阶段。'
        print(json.dumps({'ok': False, 'error': {'code': code, 'message': message,
                                                'details': safe_metadata(getattr(exc, 'details', {}))}}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
