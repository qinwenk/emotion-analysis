"""Use the installer-managed runtime without changing the machine's PATH.

The PowerShell entry point passes arguments as process-local JSON. This avoids
Windows PowerShell 5.1's lossy native quoting for contact names and search text.
Environment checks use a disposable ChatLab profile, never the user's sessions.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

REQUEST_VAR = 'EMOTION_ANALYSIS_LAUNCH_REQUEST'
SKILL_ROOT = Path(__file__).resolve().parents[1]


class LaunchError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def load_runtime(skill_root=SKILL_ROOT):
    try:
        config = json.loads((Path(skill_root) / 'runtime.json').read_text(encoding='utf-8-sig'))
        if not isinstance(config, dict) or config.get('schema') != 1:
            raise ValueError('schema')
        paths = {}
        for name in ('runtime_root', 'python', 'node', 'clb'):
            value = config[name]
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError('path')
            paths[name] = Path(value).resolve(strict=True)
        root = paths['runtime_root']
        if not root.is_dir():
            raise ValueError('root')
        for name in ('python', 'node', 'clb'):
            if not paths[name].is_file() or root not in paths[name].parents:
                raise ValueError('outside managed runtime')
        entry = paths['clb'].parent / 'node_modules/chatlab-cli/bin/chatlab.mjs'
        if not entry.is_file() or root not in entry.resolve().parents:
            raise ValueError('entry')
        paths['entry'] = entry
        return paths
    except (OSError, ValueError, KeyError, TypeError):
        raise LaunchError('runtime_not_ready', '运行环境配置缺失或损坏，请重新运行下载包中的 Install.cmd。') from None


def child_environment(runtime, current=None):
    env = dict(os.environ if current is None else current)
    env.pop(REQUEST_VAR, None)
    old_path = next((v for k, v in env.items() if k.upper() == 'PATH'), '')
    for key in list(env):
        if key.upper() == 'PATH':
            del env[key]
    env['PATH'] = os.pathsep.join([str(runtime['node'].parent), str(runtime['python'].parent),
                                  str(runtime['clb'].parent), old_path])
    env['PYTHONUTF8'] = '1'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    return env


def build_command(runtime, tool, arguments, skill_root=SKILL_ROOT):
    if not isinstance(arguments, list) or any(not isinstance(arg, str) or '\0' in arg for arg in arguments):
        raise LaunchError('invalid_arguments', '命令参数必须是文字数组。')
    args = list(arguments)
    if tool == 'chatlab':
        return [str(runtime['node']), str(runtime['entry']), *(args or ['--help'])]
    if tool != 'workflow':
        raise LaunchError('invalid_tool', '只支持 workflow、chatlab 或 check。')
    if not args:
        args = ['--help']
    if args[0] in ('run', 'resume') and not any(a == '--clb' or a.startswith('--clb=') for a in args):
        args += ['--clb', str(runtime['clb'])]
    return [str(runtime['python']), '-B', '-X', 'utf8', str(Path(skill_root) / 'scripts/desktop_workflow.py'), *args]


def _capture(command, env):
    try:
        result = subprocess.run(command, env=env, capture_output=True, text=True,
                                encoding='utf-8', errors='strict', timeout=90, shell=False,
                                **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise LaunchError('runtime_check_failed', '环境检查未完成，请重跑安装器，或将本错误交给助手排查。') from None
    if result.returncode != 0:
        raise LaunchError('runtime_check_failed', '某项依赖无法运行，请重跑安装器修复。')
    return result.stdout.strip()


def check_runtime(runtime, current=None):
    env = child_environment(runtime, current)
    try:
        python_info = json.loads(_capture([str(runtime['python']), '-B', '-X', 'utf8', '-c',
            'import sys,struct,json,cryptography,zstandard; print(json.dumps({'
            '"version":list(sys.version_info[:3]),"bits":struct.calcsize("P")*8,'
            '"cryptography":cryptography.__version__,"zstandard":zstandard.__version__}))'], env))
        if tuple(python_info['version']) < (3, 10) or python_info['bits'] != 64:
            raise ValueError('python')
        node_version = _capture([str(runtime['node']), '--version'], env)
        if tuple(int(n) for n in node_version.lstrip('v').split('.')) < (22, 19, 0):
            raise ValueError('node')
        checks_root = runtime['runtime_root'] / 'checks'
        checks_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='health-', dir=checks_root) as tmp:
            profile = Path(tmp) / 'profile'
            profile.mkdir()
            env.update(USERPROFILE=str(profile), APPDATA=str(profile / 'AppData/Roaming'),
                       LOCALAPPDATA=str(profile / 'AppData/Local'), CHATLAB_DATA_DIR=str(Path(tmp) / 'data'))
            observed_home = _capture([str(runtime['node']), '-p', 'require("node:os").homedir()'], env)
            if Path(observed_home).resolve() != profile.resolve():
                raise LaunchError('check_not_isolated', '未能隔离环境检查，已停止，未运行 ChatLab。')
            manifest = json.loads(_capture([str(runtime['node']), str(runtime['entry']), 'manifest'], env))
            if manifest.get('version') != '0.37.7' or manifest.get('apiVersion') != 1:
                raise ValueError('cli')
            sqlite_probe = _capture([str(runtime['node']), '-e',
                'const Database=require(require.resolve("better-sqlite3",{paths:[process.argv[1]]}));'
                'const db=new Database(":memory:");db.exec("CREATE TABLE health(value INTEGER); INSERT INTO health VALUES(1)");'
                'const row=db.prepare("SELECT value FROM health").get();db.close();console.log(JSON.stringify(row));',
                str(runtime['entry'].parent.parent)], env)
            if json.loads(sqlite_probe) != {'value': 1}:
                raise ValueError('sqlite')
    except (ValueError, KeyError, TypeError, OSError):
        raise LaunchError('runtime_check_failed', '运行环境版本或结构不匹配，请重新运行安装器。') from None
    return {'ok': True, 'python': '.'.join(map(str, python_info['version'])), 'bits': python_info['bits'],
            'node': node_version, 'chatlab': manifest['version'],
            'cryptography': python_info['cryptography'], 'zstandard': python_info['zstandard'],
            'sqlite_read_write': True, 'wechat_accessed': False, 'user_chatlab_accessed': False}


def main():
    try:
        try:
            request = json.loads(os.environ.get(REQUEST_VAR, '{}'))
            if not isinstance(request, dict):
                raise ValueError('request')
        except ValueError:
            raise LaunchError('invalid_request', '请通过 Run-Workflow.ps1 启动。') from None
        runtime = load_runtime()
        tool = request.get('tool', 'workflow')
        args = request.get('arguments', [])
        if tool == 'check':
            if args != []:
                raise LaunchError('invalid_arguments', '环境检查不接受额外参数。')
            print(json.dumps(check_runtime(runtime), ensure_ascii=False))
            return 0
        command = build_command(runtime, tool, args)
        return subprocess.run(command, env=child_environment(runtime), shell=False,
                              stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr,
                              **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {})).returncode
    except LaunchError as exc:
        print(json.dumps({'ok': False, 'error': {'code': exc.code, 'message': str(exc)}}, ensure_ascii=False))
        return 2
    except (OSError, ValueError):
        print(json.dumps({'ok': False, 'error': {'code': 'launch_failed', 'message': '启动失败，请运行环境检查或重跑安装器。'}}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
