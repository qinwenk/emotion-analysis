[CmdletBinding()]
param([string]$SkillsRoot, [string]$RuntimeRoot, [switch]$SkillsOnly)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'setup/Installer.Core.ps1')
$stage = 'Checking installation paths'
try {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or -not [Environment]::Is64BitOperatingSystem) { throw 'This installer supports 64-bit Windows only.' }
    $codexBase = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
    if ([string]::IsNullOrWhiteSpace($SkillsRoot)) { $SkillsRoot = Join-Path $codexBase 'skills' }
    $SkillsRoot = Get-InstallFullPath $SkillsRoot
    if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) { $RuntimeRoot = Join-Path (Split-Path -Parent $SkillsRoot) 'skill-runtimes/emotion-analysis' }
    $RuntimeRoot = Get-InstallFullPath $RuntimeRoot
    $sourceRoot = Get-InstallFullPath $PSScriptRoot
    if ((Test-InstallOverlap $sourceRoot $SkillsRoot) -or (Test-InstallOverlap $sourceRoot $RuntimeRoot) -or (Test-InstallOverlap $SkillsRoot $RuntimeRoot)) { throw 'Source package, skill destination and runtime directory must be separate folders.' }
    $skillNames = @('chat-relationship-workflow', 'emotion-analysis-evidence', 'emotion-analysis-advisor')
    foreach ($name in $skillNames) {
        $source = Join-Path $sourceRoot ('skills/' + $name)
        if (-not (Test-Path -LiteralPath (Join-Path $source 'SKILL.md') -PathType Leaf)) { throw ('Incomplete download: missing skills/' + $name + '/SKILL.md. Extract the entire repository ZIP first.') }
        $null = Get-InstallSourceFiles $source
        $null = Get-InstallFullPath (Join-Path $SkillsRoot $name)
    }
    New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null
    $lockStream = $null
    try { $lockStream = [IO.File]::Open((Join-Path $RuntimeRoot 'install.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { throw 'Another installation is using this runtime directory. Wait for it to finish and retry.' }
    try {
        $runtime = $null
        if (-not $SkillsOnly) {
            $stage = 'Installing the private runtime'
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            $spec = Get-Content -LiteralPath (Join-Path $sourceRoot 'setup/runtime.lock.json') -Raw -Encoding UTF8 | ConvertFrom-Json
            $cache = Join-Path $RuntimeRoot 'downloads'
            New-Item -ItemType Directory -Path $cache -Force | Out-Null
            Write-Host '[1/4] Preparing private Python and Node runtimes...'
            $uvArchive = Get-VerifiedArchive $spec.uv.url $spec.uv.sha256 (Join-Path $cache ('uv-' + $spec.uv.version + '.zip'))
            $uvDir = Join-Path $RuntimeRoot ('uv-' + $spec.uv.version)
            $uv = Join-Path $uvDir 'uv.exe'
            Ensure-InstallArchive $uvArchive $uvDir (Join-Path $uvDir '.archive-complete-sha256') @($uv)
            $nodeArchive = Get-VerifiedArchive $spec.node.url $spec.node.sha256 (Join-Path $cache ('node-' + $spec.node.version + '.zip'))
            $nodeDir = Join-Path $RuntimeRoot $spec.node.directory
            $node = Join-Path $nodeDir 'node.exe'
            $npm = Join-Path $nodeDir 'node_modules/npm/bin/npm-cli.js'
            Ensure-InstallArchive $nodeArchive $RuntimeRoot (Join-Path $nodeDir '.archive-complete-sha256') @($node, $npm, (Join-Path $nodeDir 'node_modules/npm/package.json'))
            $childEnv = @{
                'UV_CACHE_DIR'=(Join-Path $RuntimeRoot 'cache/uv'); 'UV_PYTHON_INSTALL_DIR'=(Join-Path $RuntimeRoot 'python');
                'UV_NO_PROGRESS'='1'; 'UV_NO_CONFIG'='1'; 'UV_PYTHON_BIN_DIR'=(Join-Path $RuntimeRoot 'python-bin');
                'PATH'=($nodeDir + ';' + $env:PATH); 'PYTHONUTF8'='1'; 'PYTHONDONTWRITEBYTECODE'='1'
            }
            $uvVersion = Invoke-InstallProcess $uv @('--version') $childEnv $RuntimeRoot 'Checking uv'
            if (-not $uvVersion.StartsWith('uv ' + $spec.uv.version + ' ')) { throw 'Unexpected private uv version. Use a fresh RuntimeRoot to repair it.' }
            $nodeVersion = Invoke-InstallProcess $node @('--version') $childEnv $RuntimeRoot 'Checking Node'
            if ($nodeVersion -ne ('v' + $spec.node.version)) { throw 'Unexpected private Node version. Use a fresh RuntimeRoot to repair it.' }
            $null = Invoke-InstallProcess $uv @('python', 'install', $spec.python, '--no-bin', '--no-registry', '--install-dir', $childEnv.UV_PYTHON_INSTALL_DIR) $childEnv $RuntimeRoot 'Downloading managed CPython'
            $basePython = Invoke-InstallProcess $uv @('python', 'find', '--managed-python', '--no-project', $spec.python) $childEnv $RuntimeRoot 'Locating managed CPython'
            $venv = Join-Path $RuntimeRoot ('venv-' + $spec.python)
            $python = Join-Path $venv 'Scripts/python.exe'
            if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { $null = Invoke-InstallProcess $uv @('venv', '--python', $basePython, $venv) $childEnv $RuntimeRoot 'Creating private Python environment' }
            Write-Host '[2/4] Installing Python libraries and ChatLab...'
            $null = Invoke-InstallProcess $uv @('pip', 'sync', '--python', $python, (Join-Path $sourceRoot 'setup/python-requirements.lock.txt')) $childEnv $RuntimeRoot 'Installing Python dependencies'
            $npmPrefix = Join-Path $RuntimeRoot ('chatlab-' + $spec.chatlab)
            New-Item -ItemType Directory -Path $npmPrefix -Force | Out-Null
            $packageLock = Join-Path $sourceRoot 'setup/chatlab/package-lock.json'
            $lockHash = (Get-InstallSha256 $packageLock) + ':sqlite-' + $spec.sqlite_native.sha256
            $marker = Join-Path $npmPrefix '.installed-lock-sha256'
            $entry = Join-Path $npmPrefix 'node_modules/chatlab-cli/bin/chatlab.mjs'
            $npmConfig = Join-Path $RuntimeRoot 'npm-empty.cfg'
            if (-not (Test-Path -LiteralPath $npmConfig)) { [IO.File]::WriteAllText($npmConfig, '') }
            $npmGlobalConfig = Join-Path $RuntimeRoot 'npm-empty-global.cfg'
            if (-not (Test-Path -LiteralPath $npmGlobalConfig)) { [IO.File]::WriteAllText($npmGlobalConfig, '') }
            $childEnv['npm_config_userconfig'] = $npmConfig
            $childEnv['npm_config_globalconfig'] = $npmGlobalConfig
            $childEnv['npm_config_cache'] = Join-Path $RuntimeRoot 'cache/npm'
            $alreadyInstalled = (Test-Path -LiteralPath $marker -PathType Leaf) -and ((Get-Content -LiteralPath $marker -Raw).Trim() -eq $lockHash) -and (Test-Path -LiteralPath $entry -PathType Leaf)
            $installChatLab = {
                Copy-Item -LiteralPath (Join-Path $sourceRoot 'setup/chatlab/package.json') -Destination (Join-Path $npmPrefix 'package.json') -Force
                Copy-Item -LiteralPath $packageLock -Destination (Join-Path $npmPrefix 'package-lock.json') -Force
                $null = Invoke-InstallProcess $node @($npm, 'ci', '--prefix', $npmPrefix, '--ignore-scripts', '--no-audit', '--no-fund') $childEnv $RuntimeRoot 'Installing ChatLab without compiler hooks' 1200
                $sqliteDir = Join-Path $npmPrefix 'node_modules/better-sqlite3'
                $sqlitePackage = Get-Content -LiteralPath (Join-Path $sqliteDir 'package.json') -Raw -Encoding UTF8 | ConvertFrom-Json
                $nodeAbi = Invoke-InstallProcess $node @('-p','process.versions.modules') $childEnv $RuntimeRoot 'Checking Node native ABI'
                if ($sqlitePackage.version -ne $spec.sqlite_native.package_version -or $nodeAbi -ne $spec.sqlite_native.node_abi) { throw 'The locked SQLite binary does not match the installed package or Node ABI. Update the installer lock files together.' }
                $nativeArchive = Get-VerifiedArchive $spec.sqlite_native.url $spec.sqlite_native.sha256 (Join-Path $cache ('better-sqlite3-' + $spec.sqlite_native.package_version + '-node-' + $spec.sqlite_native.node_abi + '-win32-x64.tar.gz'))
                $null = Invoke-InstallProcess $python @('-B','-X','utf8',(Join-Path $sourceRoot 'setup/extract_native.py'),'--archive',$nativeArchive,'--destination',$sqliteDir,'--sha256',$spec.sqlite_native.sha256) $childEnv $RuntimeRoot 'Installing the verified SQLite native binary'
            }
            # The desktop importer expects npm's global-layout shim.
            $clb = Join-Path $npmPrefix 'clb.cmd'
            [IO.File]::WriteAllText($clb, '@echo off' + "`r`n" + '"%~dp0..\' + $spec.node.directory + '\node.exe" "%~dp0node_modules\chatlab-cli\bin\chatlab.mjs" %*' + "`r`n", (New-Object Text.UTF8Encoding($false)))
            Write-Host '[3/4] Checking dependencies in an isolated test profile...'
            $pythonCheck = Invoke-InstallProcess $python @('-X','utf8','-c','import sys,struct,cryptography,zstandard; assert sys.version_info[:3] == (3,12,12); assert struct.calcsize("P") == 8; print("python-ready")') $childEnv $RuntimeRoot 'Checking Python libraries'
            if ($pythonCheck -ne 'python-ready') { throw 'Python environment verification failed.' }
            $verifyChatLab = { Test-InstallChatLab $node $entry $RuntimeRoot $childEnv $spec.chatlab (Join-Path $sourceRoot 'examples/synthetic-chat.json') }
            $null = Invoke-VerifiedPackageSetup $alreadyInstalled $marker $lockHash $installChatLab $verifyChatLab
            $runtime = [ordered]@{schema=1;python=$python;node=$node;clb=$clb;runtime_root=$RuntimeRoot;python_version=$spec.python;node_version=$spec.node.version;chatlab_version=$spec.chatlab}
        }
        $stage = 'Installing the workflow and companion skills'
        Write-Host '[4/4] Installing workflow and companion skills...'
        foreach ($name in $skillNames) { Install-WorkflowSkill (Join-Path $sourceRoot ('skills/' + $name)) (Join-Path $SkillsRoot $name) (Join-Path $RuntimeRoot 'backups') }
        if ($runtime) {
            $runtimeFile = Join-Path $SkillsRoot 'chat-relationship-workflow/runtime.json'
            [IO.File]::WriteAllText($runtimeFile, ($runtime | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))
        }
        Write-Host 'Installation complete.'
        Write-Host ('Skills: ' + $SkillsRoot)
        if ($SkillsOnly) { Write-Host 'Skills-only mode: runtime configuration was not installed or verified.' }
        else { Write-Host ('Private runtime: ' + $RuntimeRoot); Write-Host 'Three skills and the runtime are ready. Start a new Codex turn to use them.' }
        Write-Host 'No WeChat process, chat database or normal ChatLab session was accessed.'
    } finally { if ($lockStream) { $lockStream.Dispose() } }
} catch {
    Write-Error ("Installation stopped during {0}: {1}" -f $stage, $_.Exception.Message)
    exit 1
}
