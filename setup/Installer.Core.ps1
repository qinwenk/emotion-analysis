# Windows PowerShell 5.1-compatible helpers. No global PATH or registry changes.
Set-StrictMode -Version 2.0

function Get-InstallSha256([string]$Path) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try { return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $algorithm.Dispose() }
}

function Get-InstallFullPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { throw 'An installation path is missing.' }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    if ($full.StartsWith('\\') -or $full.Length -le 3) { throw 'Use a local folder, not a network share or drive root.' }
    $cursor = $full
    while ($cursor -and (Split-Path -Parent $cursor)) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Installation paths cannot traverse links or junctions.' }
        }
        $cursor = Split-Path -Parent $cursor
    }
    return $full
}

function Test-InstallOverlap([string]$First, [string]$Second) {
    $a = [IO.Path]::GetFullPath($First).TrimEnd('\', '/')
    $b = [IO.Path]::GetFullPath($Second).TrimEnd('\', '/')
    return $a.Equals($b, [StringComparison]::OrdinalIgnoreCase) -or $a.StartsWith($b + '\', [StringComparison]::OrdinalIgnoreCase) -or $b.StartsWith($a + '\', [StringComparison]::OrdinalIgnoreCase)
}

function ConvertTo-InstallArgument([string]$Value) {
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    return '"' + ([regex]::Replace([regex]::Replace($Value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1')) + '"'
}

function Invoke-InstallProcess {
    param([string]$File, [string[]]$Arguments, [hashtable]$Environment = @{}, [string]$WorkingDirectory, [string]$Stage, [int]$TimeoutSeconds = 900)
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $File
    $start.Arguments = (($Arguments | ForEach-Object { ConvertTo-InstallArgument $_ }) -join ' ')
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = New-Object Text.UTF8Encoding($false)
    $start.StandardErrorEncoding = New-Object Text.UTF8Encoding($false)
    if ($WorkingDirectory) { $start.WorkingDirectory = $WorkingDirectory }
    foreach ($key in $Environment.Keys) { $start.EnvironmentVariables[$key] = [string]$Environment[$key] }
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw "$Stage could not start." }
        $outTask = $process.StandardOutput.ReadToEndAsync()
        $errTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            # Only terminate the process tree created by this invocation.
            $stopInfo = New-Object Diagnostics.ProcessStartInfo
            $stopInfo.FileName = Join-Path ([Environment]::GetFolderPath('System')) 'taskkill.exe'
            $stopInfo.Arguments = '/PID ' + $process.Id + ' /T /F'
            $stopInfo.UseShellExecute = $false
            $stopInfo.CreateNoWindow = $true
            $stopInfo.RedirectStandardOutput = $true
            $stopInfo.RedirectStandardError = $true
            $stopProcess = [Diagnostics.Process]::Start($stopInfo)
            try {
                if (-not $stopProcess.WaitForExit(15000)) { $stopProcess.Kill() }
            } finally { $stopProcess.Dispose() }
            if (-not $process.HasExited) { $process.Kill() }
            $process.WaitForExit()
            throw "$Stage timed out. Check the connection, then run the installer again."
        }
        $stdout = $outTask.GetAwaiter().GetResult()
        $stderr = $errTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            $detail = $stderr.Trim()
            if ($detail.Length -gt 2500) { $detail = $detail.Substring($detail.Length - 2500) }
            throw ("{0} failed (exit {1}). Check the connection and available disk space, then retry. {2}" -f $Stage, $process.ExitCode, $detail)
        }
        return $stdout.Trim()
    } finally { $process.Dispose() }
}

function Get-VerifiedArchive {
    param([string]$Url, [string]$Sha256, [string]$Destination)
    if ((Test-Path -LiteralPath $Destination -PathType Leaf) -and ((Get-InstallSha256 $Destination) -eq $Sha256)) { return $Destination }
    $partial = $Destination + '.part-' + [Guid]::NewGuid().ToString('N')
    try {
        Invoke-WebRequest -Uri $Url -OutFile $partial -UseBasicParsing -TimeoutSec 300
        if ((Get-InstallSha256 $partial) -ne $Sha256) { throw 'Downloaded runtime checksum does not match. No downloaded program has been executed.' }
        Move-Item -LiteralPath $partial -Destination $Destination -Force
    } catch { throw ("Runtime download failed: {0}. Retry on a working connection; do not disable checksum validation." -f $_.Exception.Message) }
    return $Destination
}

function Expand-VerifiedArchive([string]$Archive, [string]$Destination) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $target = [IO.Path]::GetFullPath($Destination).TrimEnd('\') + '\'
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $zip.Entries) {
            $entryPath = [IO.Path]::GetFullPath((Join-Path $Destination $entry.FullName))
            if (-not $entryPath.StartsWith($target, [StringComparison]::OrdinalIgnoreCase)) { throw 'Archive entry escapes its installation directory.' }
        }
        # .NET extraction also works when a host does not expose the Archive module.
        foreach ($entry in $zip.Entries) {
            $entryPath = [IO.Path]::GetFullPath((Join-Path $Destination $entry.FullName))
            if ($entry.FullName.EndsWith('/')) { [IO.Directory]::CreateDirectory($entryPath) | Out-Null; continue }
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($entryPath)) | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $entryPath, $true)
        }
    } finally { $zip.Dispose() }
}

function Get-InstallSourceFiles([string]$Directory) {
    $files = @(Get-ChildItem -LiteralPath $Directory -File -Recurse -Force | Where-Object { $_.FullName -notmatch '[\\/]__pycache__[\\/]' -and $_.Extension -notin @('.pyc', '.pyo') -and $_.Name -ne 'runtime.json' })
    foreach ($file in $files) { $null = Get-InstallFullPath $file.FullName }
    return $files
}

function Ensure-InstallArchive {
    param([string]$Archive, [string]$Destination, [string]$Marker, [string[]]$RequiredFiles)
    $hash = Get-InstallSha256 $Archive
    $complete = (Test-Path -LiteralPath $Marker -PathType Leaf) -and ((Get-Content -LiteralPath $Marker -Raw).Trim() -eq $hash)
    foreach ($required in $RequiredFiles) { if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { $complete = $false } }
    if ($complete) { return }
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Marker)) | Out-Null
    [IO.File]::WriteAllText($Marker, 'incomplete')
    Expand-VerifiedArchive $Archive $Destination
    foreach ($required in $RequiredFiles) { if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw 'Runtime archive is incomplete after extraction.' } }
    [IO.File]::WriteAllText($Marker, $hash)
}

function Invoke-VerifiedPackageSetup {
    param([bool]$CacheValid, [string]$Marker, [string]$Hash, [scriptblock]$Install, [scriptblock]$Verify)
    if ($CacheValid) {
        try { $null = & $Verify; return 'reused' }
        catch { Write-Host 'Repairing the private ChatLab installation...' }
    }
    [IO.File]::WriteAllText($Marker, 'incomplete')
    $null = & $Install
    # A failure here leaves the marker invalid; there is no retry loop.
    $null = & $Verify
    [IO.File]::WriteAllText($Marker, $Hash)
    return 'installed'
}

function Test-InstallChatLab {
    param([string]$Node, [string]$Entry, [string]$RuntimeRoot, [hashtable]$Environment, [string]$Version, [string]$SyntheticFile)
    $checkRoot = Join-Path $RuntimeRoot ('checks/' + [Guid]::NewGuid().ToString('N'))
    $checkProfile = Join-Path $checkRoot 'profile'
    New-Item -ItemType Directory -Path $checkProfile -Force | Out-Null
    $verifyEnv = @{}
    foreach ($key in $Environment.Keys) { $verifyEnv[$key] = $Environment[$key] }
    $verifyEnv['USERPROFILE'] = $checkProfile
    $verifyEnv['APPDATA'] = Join-Path $checkProfile 'AppData/Roaming'
    $verifyEnv['LOCALAPPDATA'] = Join-Path $checkProfile 'AppData/Local'
    $verifyEnv['CHATLAB_DATA_DIR'] = Join-Path $checkRoot 'chatlab-data'
    $actualHome = Invoke-InstallProcess $Node @('-e','console.log(require("os").homedir())') $verifyEnv $RuntimeRoot 'Checking test-profile isolation'
    if ($actualHome -ne $checkProfile) { throw 'Cannot verify ChatLab test-profile isolation.' }
    $manifest = (Invoke-InstallProcess $Node @($Entry, 'manifest') $verifyEnv $RuntimeRoot 'Checking ChatLab') | ConvertFrom-Json
    if ($manifest.version -ne $Version -or $manifest.apiVersion -ne 1) { throw 'ChatLab version or API verification failed.' }
    $validated = (Invoke-InstallProcess $Node @($Entry, 'validate', $SyntheticFile, '--json') $verifyEnv $RuntimeRoot 'Checking synthetic import format') | ConvertFrom-Json
    if ($validated.ok -ne $true -or $validated.data.valid -ne $true -or $validated.data.errorCount -ne 0) { throw 'Synthetic import format validation failed.' }
    $sessions = (Invoke-InstallProcess $Node @($Entry, 'sessions', 'list', '--format', 'json') $verifyEnv $RuntimeRoot 'Checking isolated SQLite support') | ConvertFrom-Json
    if ($sessions.ok -ne $true -or @($sessions.data.items).Count -ne 0) { throw 'Isolated ChatLab database verification failed.' }
    $sessionId = 'emotion-install-selftest'
    $imported = (Invoke-InstallProcess $Node @($Entry, 'import', $SyntheticFile, '--format', 'chatlab', '--session-id', $sessionId, '--json') $verifyEnv $RuntimeRoot 'Importing five synthetic messages in the isolated profile') | ConvertFrom-Json
    if ($imported.ok -ne $true -or $imported.data.success -ne $true -or $imported.data.sessionId -ne $sessionId -or $imported.data.newMessageCount -ne 5 -or $imported.data.diagnostics.messagesSkipped -ne 0) { throw 'Isolated synthetic import failed.' }
    $count = (Invoke-InstallProcess $Node @($Entry, 'sql', 'SELECT COUNT(*) AS total FROM message', '--session', $sessionId, '--limit', '10', '--format', 'json') $verifyEnv $RuntimeRoot 'Reading the isolated synthetic message count') | ConvertFrom-Json
    if ($count.ok -ne $true -or $count.meta.session.id -ne $sessionId -or $count.data.truncated -or @($count.data.rows).Count -ne 1 -or $count.data.rows[0].total -ne 5) { throw 'Isolated SQLite read/write verification failed.' }
}

function Install-WorkflowSkill {
    param([string]$Source, [string]$Destination, [string]$BackupRoot)
    $files = @(Get-InstallSourceFiles $Source)
    $changed = $false
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($Source.Length).TrimStart('\')
        $target = Join-Path $Destination $relative
        $null = Get-InstallFullPath $target
        if (-not (Test-Path -LiteralPath $target -PathType Leaf) -or (Get-InstallSha256 $target) -ne (Get-InstallSha256 $file.FullName)) { $changed = $true }
    }
    if (-not $changed) { Write-Host ('Already current: ' + (Split-Path -Leaf $Source)); return }
    if (Test-Path -LiteralPath $Destination) {
        $backup = Join-Path $BackupRoot ((Split-Path -Leaf $Source) + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 6))
        New-Item -ItemType Directory -Path $backup -Force | Out-Null
        Get-ChildItem -LiteralPath $Destination -Force | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $backup -Recurse -Force }
        Write-Host ('Previous skill backup: ' + $backup)
    }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($Source.Length).TrimStart('\')
        $target = Join-Path $Destination $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $target -Force
        if ((Get-InstallSha256 $target) -ne (Get-InstallSha256 $file.FullName)) { throw ('Installed skill verification failed: ' + $relative) }
    }
    Write-Host ('Installed and verified: ' + (Split-Path -Leaf $Source))
}
