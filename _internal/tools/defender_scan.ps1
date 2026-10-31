# Build-only, read-only artifact inspection. This never changes Defender policy.
# Microsoft documents -DisableRemediation as a CUSTOM-SCAN option: it scans
# archives, ignores exclusions, and reports detections without changing files.
# It does not disable antivirus protection. Nonzero/error/unavailable is NOT clean.
# https://learn.microsoft.com/defender-endpoint/command-line-arguments-microsoft-defender-antivirus
[CmdletBinding()]
param(
    [string[]] $ScanPath = @(),
    [string] $ReportPath,
    [ValidateRange(60, 600)] [int] $TimeoutSeconds = 480
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-UnifiedDefenderStatus {
    # Read the Microsoft CIM provider directly, avoiding module discovery from
    # a user-controlled PSModulePath and WinPS compatibility child processes.
    $value = Get-CimInstance -Namespace 'root/Microsoft/Windows/Defender' -ClassName 'MSFT_MpComputerStatus' -ErrorAction Stop
    [ordered]@{
        AMRunningMode = [string] $value.AMRunningMode
        AMServiceEnabled = [bool] $value.AMServiceEnabled
        AntivirusEnabled = [bool] $value.AntivirusEnabled
        RealTimeProtectionEnabled = [bool] $value.RealTimeProtectionEnabled
        AMEngineVersion = [string] $value.AMEngineVersion
        AMProductVersion = [string] $value.AMProductVersion
        AntivirusSignatureVersion = [string] $value.AntivirusSignatureVersion
        AntivirusSignatureLastUpdated = $value.AntivirusSignatureLastUpdated.ToUniversalTime().ToString('o')
    }
}

function Test-UnifiedDefenderActive($Status) {
    return ($Status.AMRunningMode -eq 'Normal' -and $Status.AMServiceEnabled -and $Status.AntivirusEnabled)
}

function Assert-UnifiedOrdinaryPath([string] $Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Artifact paths must not contain links or reparse points.'
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if ($parent -eq $current) { break }
        $current = $parent
    }
}

function Resolve-UnifiedDefenderExecutable {
    # Do not resolve MpCmdRun through PATH. Inspect only documented OS-owned
    # locations and verify the selected executable's Microsoft signature.
    $candidates = @()
    $platform = Join-Path $env:ProgramData 'Microsoft/Windows Defender/Platform'
    if (Test-Path -LiteralPath $platform -PathType Container) {
        foreach ($folder in Get-ChildItem -LiteralPath $platform -Directory -Force) {
            if ($folder.Name -match '^\d+\.\d+\.\d+\.\d+(?:-\d+)?$') {
                $version = [version] ($folder.Name -replace '-\d+$', '')
                $candidates += [pscustomobject]@{ Version = $version; Path = (Join-Path $folder.FullName 'MpCmdRun.exe') }
            }
        }
    }
    $candidates = @($candidates | Sort-Object Version -Descending)
    $candidates += [pscustomobject]@{ Version = [version] '0.0'; Path = (Join-Path $env:ProgramFiles 'Windows Defender/MpCmdRun.exe') }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate.Path -PathType Leaf) {
            Assert-UnifiedOrdinaryPath $candidate.Path
            $signature = Get-AuthenticodeSignature -LiteralPath $candidate.Path -ErrorAction Stop
            if ($signature.Status -ne 'Valid' -or
                $signature.SignerCertificate.Subject -notmatch '(^|,\s*)O=Microsoft Corporation(,|$)') {
                throw 'The operating-system Defender executable lacks a valid Microsoft signature.'
            }
            return [IO.Path]::GetFullPath($candidate.Path)
        }
    }
    throw 'Microsoft Defender MpCmdRun.exe is unavailable.'
}

function Invoke-UnifiedDefenderCommand([string] $Executable, [string[]] $Argument, [int] $TimeoutMilliseconds) {
    if ($PSVersionTable.PSVersion.Major -lt 7) {
        throw 'The build artifact scan requires PowerShell 7 or later.'
    }
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $Executable
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    foreach ($item in $Argument) { $start.ArgumentList.Add($item) }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw 'Could not start the Defender custom scan.' }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            # Stop only this timed-out command process. This does not stop
            # the antivirus service, change a policy, or cancel other scans.
            $process.Kill()
            $process.WaitForExit()
            throw 'The Defender command timed out; no clean verdict is available.'
        }
        return [pscustomobject]@{ exit_code = $process.ExitCode; stdout = $stdout.GetAwaiter().GetResult(); stderr = $stderr.GetAwaiter().GetResult() }
    }
    finally { $process.Dispose() }
}

function Get-UnifiedArtifactInventory([string[]] $Targets) {
    $entries = [Collections.Generic.List[object]]::new()
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $pending = [Collections.Generic.Queue[string]]::new()
    foreach ($target in $Targets) {
        Assert-UnifiedOrdinaryPath $target
        $pending.Enqueue([IO.Path]::GetFullPath($target))
    }
    while ($pending.Count) {
        $path = $pending.Dequeue()
        $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Artifact trees must not contain links or reparse points.' }
        if ($item.PSIsContainer) {
            foreach ($child in Get-ChildItem -LiteralPath $item.FullName -Force) { $pending.Enqueue($child.FullName) }
        }
        elseif ($seen.Add($item.FullName)) {
            if ($seen.Count -gt 50000) { throw 'Artifact inventory exceeds its file-count limit.' }
            $entries.Add([pscustomobject]@{ path = $item.FullName; bytes = $item.Length; sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant() })
        }
    }
    return @($entries | Sort-Object path)
}

function Invoke-UnifiedArtifactScan([string[]] $Targets, [string] $Output, [int] $BudgetSeconds = 480) {
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $report = [ordered]@{
        schema = 1
        scanner = 'Microsoft Defender Antivirus custom scan'
        started_at = [DateTime]::UtcNow.ToString('o')
        completed_at = $null
        status = 'not_completed'
        clean = $false
        reason = $null
        defender_before = $null
        defender_after = $null
        scanner_executable = $null
        signature_update = $null
        scans = @()
        threat_names = @()
        artifact_inventory_before = @()
        artifact_inventory_after = @()
        artifacts_unchanged = $false
        coverage = 'Static custom scan of the specified release artifacts; not a SmartScreen reputation or runtime behavior assessment.'
    }
    $exitCode = 3
    try {
        if (-not $Targets -or $Targets.Count -gt 64 -or [string]::IsNullOrWhiteSpace($Output)) { throw 'Specify one to sixty-four literal artifact targets and a report file.' }
        if ($BudgetSeconds -lt 60 -or $BudgetSeconds -gt 600) { throw 'The scan deadline must be between sixty and six hundred seconds.' }
        $Targets = @($Targets | ForEach-Object { [IO.Path]::GetFullPath($_) })
        $report.artifact_inventory_before = @(Get-UnifiedArtifactInventory $Targets)
        if (-not $report.artifact_inventory_before.Count) { throw 'The scan must cover at least one ordinary artifact file.' }
        $report.status = 'unavailable'
        $report.defender_before = Get-UnifiedDefenderStatus
        if (-not (Test-UnifiedDefenderActive $report.defender_before)) { throw 'Defender is not active in Normal antivirus mode; this is not a clean scan.' }
        $scanner = Resolve-UnifiedDefenderExecutable
        $report.scanner_executable = $scanner
        $report.status = 'signature_update_failed'
        $remaining = [math]::Min(120000, [int] ($BudgetSeconds * 1000 - $clock.ElapsedMilliseconds))
        if ($remaining -le 0) { throw 'The scan deadline elapsed before updating security intelligence.' }
        $report.signature_update = Invoke-UnifiedDefenderCommand $scanner @('-SignatureUpdate') $remaining
        if ($report.signature_update.exit_code -ne 0) { throw 'Defender security-intelligence update failed; the scan is not certified current.' }
        $report.defender_after = Get-UnifiedDefenderStatus
        if (-not (Test-UnifiedDefenderActive $report.defender_after)) { throw 'Defender is no longer active after its security-intelligence update.' }
        $updated = [DateTime]::Parse($report.defender_after.AntivirusSignatureLastUpdated).ToUniversalTime()
        if ($updated -lt [DateTime]::UtcNow.AddHours(-72) -or $updated -gt [DateTime]::UtcNow.AddHours(24) -or
            [string]::IsNullOrWhiteSpace($report.defender_after.AntivirusSignatureVersion)) {
            throw 'Security intelligence is stale or invalid after the update request.'
        }
        $report.status = 'scan_failed'
        foreach ($target in $Targets) {
            $remaining = [int] ($BudgetSeconds * 1000 - $clock.ElapsedMilliseconds)
            if ($remaining -le 0) { throw 'The artifact scan deadline elapsed.' }
            $result = Invoke-UnifiedDefenderCommand $scanner @('-Scan', '-ScanType', '3', '-File', $target, '-DisableRemediation') $remaining
            $report.scans += [pscustomobject]@{ target = $target; exit_code = $result.exit_code; stdout = $result.stdout; stderr = $result.stderr }
            # This is diagnostic extraction only: localized/unrecognized text
            # never turns a nonzero exit status into a clean result.
            foreach ($match in [regex]::Matches(($result.stdout + "`n" + $result.stderr), '(?im)^\s*(?:Threat(?:\s+Name)?|Name)\s*:\s*(.+)$')) {
                $report.threat_names += $match.Groups[1].Value.Trim()
            }
            if ($result.exit_code -ne 0) {
                $report.status = 'detected_or_scan_error'
                $exitCode = 2
                throw 'Defender returned a detection or scan error. Inspect the recorded command output; publishing is blocked.'
            }
        }
        $report.artifact_inventory_after = @(Get-UnifiedArtifactInventory $Targets)
        $before = ConvertTo-Json -InputObject $report.artifact_inventory_before -Depth 5 -Compress
        $after = ConvertTo-Json -InputObject $report.artifact_inventory_after -Depth 5 -Compress
        if ($before -cne $after) { throw 'Release artifacts changed during antivirus inspection; no clean verdict is retained.' }
        $report.artifacts_unchanged = $true
        if ($report.threat_names.Count) { $report.status = 'detected_or_scan_error'; $exitCode = 2; throw 'Defender reported a threat name despite its exit code; publishing is blocked.' }
        $report.defender_after = Get-UnifiedDefenderStatus
        if (-not (Test-UnifiedDefenderActive $report.defender_after)) { throw 'Defender stopped during the artifact scan.' }
        $report.status = 'clean'
        $report.clean = $true
        $exitCode = 0
    }
    catch { $report.reason = $_.Exception.Message }
    finally {
        $report.completed_at = [DateTime]::UtcNow.ToString('o')
        if (-not [string]::IsNullOrWhiteSpace($Output)) {
            $destination = [IO.Path]::GetFullPath($Output)
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination)) | Out-Null
            [IO.File]::WriteAllText($destination, (ConvertTo-Json -InputObject $report -Depth 10), [Text.UTF8Encoding]::new($false))
        }
        Write-Host ('Defender artifact inspection: {0}; clean={1}; unchanged={2}' -f $report.status, $report.clean, $report.artifacts_unchanged)
        if ($report.reason) { Write-Host $report.reason }
    }
    return $exitCode
}

if ($MyInvocation.InvocationName -ne '.') {
    exit (Invoke-UnifiedArtifactScan -Targets $ScanPath -Output $ReportPath -BudgetSeconds $TimeoutSeconds)
}
