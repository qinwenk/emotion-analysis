param([string]$SkillsRoot)
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($SkillsRoot)) {
    $codexBase = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
    $SkillsRoot = Join-Path $codexBase 'skills'
}
$skillName = 'chat-relationship-workflow'
$sourceDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "skills/$skillName"))
$targetRoot = [IO.Path]::GetFullPath($SkillsRoot)
$targetDir = [IO.Path]::GetFullPath((Join-Path $targetRoot $skillName))
if (-not (Test-Path -LiteralPath (Join-Path $sourceDir 'SKILL.md') -PathType Leaf)) { throw 'Missing source SKILL.md.' }
if ($sourceDir -eq $targetDir -or -not $targetDir.StartsWith($targetRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid installation destination.' }
if (Test-Path -LiteralPath $targetDir) {
    $backup = Join-Path $PSScriptRoot ('backups/' + $skillName + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    Get-ChildItem -LiteralPath $targetDir -Force | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $backup -Recurse -Force }
    Write-Output "Previous skill backup: $backup"
}
New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
Get-ChildItem -LiteralPath $sourceDir -Force | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $targetDir -Recurse -Force }
Get-ChildItem -LiteralPath $sourceDir -File -Recurse | ForEach-Object {
    $relative = $_.FullName.Substring($sourceDir.Length).TrimStart('\')
    $targetFile = Join-Path $targetDir $relative
    if ((Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $targetFile -Algorithm SHA256).Hash) { throw "Verification failed: $relative" }
}
Write-Output "Installed and verified: $targetDir"
Write-Output 'This installs the workflow only. It does not import chats, retrieve keys, or install Python/ChatLab dependencies.'
