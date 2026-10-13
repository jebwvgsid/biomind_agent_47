[CmdletBinding()]
param(
  [string]$ExePath = "$env:LOCALAPPDATA\Programs\BioMind Studio Workbench\BioMind Studio.exe",
  [string]$EvidenceDirectory = (Join-Path $env:USERPROFILE ('Desktop\BioMind-Acceptance-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))),
  [switch]$Launch,
  [switch]$Interactive
)
$ErrorActionPreference = 'Stop'
if (!(Test-Path -LiteralPath $ExePath -PathType Leaf)) { throw "EXE not found: $ExePath" }
New-Item -ItemType Directory -Path $EvidenceDirectory -ErrorAction Stop | Out-Null
$exe = Get-Item -LiteralPath $ExePath
$asarPath = Join-Path $exe.DirectoryName 'resources\app.asar'
$receipt = [ordered]@{
  schema = 'BioMindWindowsAcceptanceV1'
  time = (Get-Date).ToUniversalTime().ToString('o')
  exePath = $exe.FullName
  exeSha256 = (Get-FileHash -LiteralPath $ExePath -Algorithm SHA256).Hash.ToLowerInvariant()
  # PE version may reflect the unmodified Electron runtime. Logical application
  # identity is verified by app.asar hash against windows-expected.json.
  peProduct = $exe.VersionInfo.ProductName
  peVersion = $exe.VersionInfo.ProductVersion
  asarSha256 = $null
  expectedLogicalVersion = '1.3.0-rc.20261009.1'
  packageIdentity = 'NOT_RUN'
  windows = [Environment]::OSVersion.VersionString
  checks = @()
}
if (Test-Path -LiteralPath $asarPath) { $receipt.asarSha256 = (Get-FileHash -LiteralPath $asarPath -Algorithm SHA256).Hash.ToLowerInvariant() }
$expectedPath = Join-Path $PSScriptRoot 'windows-expected.json'
if (Test-Path -LiteralPath $expectedPath) {
  $expected = Get-Content -LiteralPath $expectedPath -Raw -Encoding UTF8 | ConvertFrom-Json
  $receipt.packageIdentity = if ($receipt.exeSha256 -eq $expected.exeSha256 -and $receipt.asarSha256 -eq $expected.asarSha256) {'PASS'} else {'FAIL'}
}
if ($Launch) { Start-Process -FilePath $ExePath | Out-Null }
$cases = @(
  @('first-setup', 'Save server configuration in the app and enter the complete sidebar/chat/preview/settings workbench.'),
  @('auto-start', 'With the configured remote service stopped, connect in the app; verify one supported start/job and actual health before ready.'),
  @('reopen-reuse', 'Close and double-click the installed EXE; verify saved connection reuse and no duplicate service/job.'),
  @('two-sessions', 'Choose a Linux project; create native sessions A and B; run/switch A-B-A, preserving IDs, project, history, pending approvals and activity.'),
  @('history-restart', 'Close/reopen the app; verify native A/B history and last focused session. Closing the window must not stop the remote task.'),
  @('real-model-tools-artifact', 'Run a real BioMind model/tool task. Record native session ID, tool events and an actual produced file; preview that file in the app.'),
  @('model-change', 'Choose a different server model; verify the backend state and next actual response use it.'),
  @('reconnect-no-replay', 'Interrupt network briefly during work; reconnect to the same native ID/owner and verify no duplicate task submission.'),
  @('slow-or-failed-start', 'Observe a delayed or failed initialization; verify pending/error state and disabled send until authoritative state recovers.'),
  @('attachments', 'Attach Windows text/image bytes; verify Linux receives the bytes and the model/tool can use them.'),
  @('server-settings', 'Change supported Skills/instructions/memory settings and read their confirmed state back from the same BioMind server.'),
  @('remote-packages', 'Use an isolated server scope in Plugins. Compare source, scope, version and native filters with Linux biomind list. Record BUSY or legacy BLOCKED honestly; do not change production packages.'),
  @('plugin-tool-display', 'Run the isolated package tool through full BioMind; inspect its name, arguments, events, result and error in the generic tool card. Record actual release ID.'),
  @('idle-owner-restore', 'After a successful package change, verify the original idle native session ID/history is restored and new events arrive; no prompt is replayed.')
)
foreach ($case in $cases) {
  $status = 'NOT_RUN'; $detail = ''
  if ($Interactive) {
    Write-Host "`n$($case[0]): $($case[1])"
    $answer = Read-Host 'Result PASS / FAIL / NOT_RUN / BLOCKED (Enter = NOT_RUN)'
    if ($answer) { $answer = $answer.Trim().ToUpperInvariant(); if ($answer -notin @('PASS','FAIL','NOT_RUN','BLOCKED')) { throw 'Invalid result; no automatic PASS is inferred.' }; $status = $answer }
    if ($status -ne 'NOT_RUN') { $detail = Read-Host 'Evidence: observed session/job IDs, project, artifact path, screenshot filenames or failure (NO secrets)' }
    if ($status -eq 'PASS' -and !$detail) { throw 'PASS requires a concrete observation/evidence description.' }
  }
  $receipt.checks += [ordered]@{ id=$case[0]; result=$status; observation=$detail }
  $receipt | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $EvidenceDirectory 'windows-acceptance.json') -Encoding UTF8
}
Write-Host "Evidence saved: $EvidenceDirectory"
Write-Host 'Hash PASS only proves package identity. Product acceptance requires the recorded manual observations.'
