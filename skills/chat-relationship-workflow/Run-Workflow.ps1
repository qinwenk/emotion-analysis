param(
    [ValidateSet('workflow', 'chatlab', 'check')]
    [string]$Tool = 'workflow',
    [string[]]$Arguments = @()
)
$ErrorActionPreference = 'Stop'
$configPath = Join-Path $PSScriptRoot 'runtime.json'
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw 'Runtime is not configured. Run Install.cmd or Install-Workflow.ps1 from the downloaded package first.'
}
$runtime = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($runtime.schema -ne 1 -or -not [IO.Path]::IsPathRooted($runtime.python) -or -not (Test-Path -LiteralPath $runtime.python -PathType Leaf)) {
    throw 'Runtime configuration is invalid. Run the package installer again.'
}
# JSON preserves literal quotes, spaces and Chinese across Windows PowerShell 5.1.
# Only the fixed script path reaches the native command line; user arguments do not.
$requestName = 'EMOTION_ANALYSIS_LAUNCH_REQUEST'
$previousRequest = [Environment]::GetEnvironmentVariable($requestName, 'Process')
$oldEncoding = [Console]::OutputEncoding
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $request = @{tool=$Tool; arguments=@($Arguments)} | ConvertTo-Json -Depth 3 -Compress
    [Environment]::SetEnvironmentVariable($requestName, $request, 'Process')
    & $runtime.python -B -X utf8 (Join-Path $PSScriptRoot 'scripts/runtime_launcher.py')
    $resultCode = $LASTEXITCODE
} finally {
    [Environment]::SetEnvironmentVariable($requestName, $previousRequest, 'Process')
    [Console]::OutputEncoding = $oldEncoding
}
if ($resultCode -ne 0) { throw "Workflow command failed (exit $resultCode). See the structured error above." }
