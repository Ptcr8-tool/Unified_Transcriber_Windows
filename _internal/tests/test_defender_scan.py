"""Exercise the release scan's decision boundary without malware or AV changes.

The actual PowerShell implementation runs with fake Defender process/status
adapters, while artifact traversal, SHA256 comparison, deadlines and JSON report
writing remain real. Native Defender itself is exercised only by Windows CI.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'tools' / 'defender_scan.ps1'
POWERSHELL = shutil.which('pwsh')

HARNESS = r'''
$ErrorActionPreference = 'Stop'
. $env:UNIFIED_TEST_SCAN_SCRIPT
$script:CommandCalls = 0
$script:StatusCalls = 0
function Get-UnifiedDefenderStatus {
    $script:StatusCalls += 1
    $active = ($env:UNIFIED_TEST_CASE -ne 'inactive')
    if ($env:UNIFIED_TEST_CASE -eq 'stopped' -and $script:StatusCalls -ge 3) { $active = $false }
    $updated = [DateTime]::UtcNow.ToString('o')
    if ($env:UNIFIED_TEST_CASE -eq 'stale') { $updated = [DateTime]::UtcNow.AddDays(-10).ToString('o') }
    [ordered]@{ AMRunningMode = $(if ($active) { 'Normal' } else { 'Passive' }); AMServiceEnabled = $active;
        AntivirusEnabled = $active; RealTimeProtectionEnabled = $active; AMEngineVersion = 'test';
        AMProductVersion = 'test'; AntivirusSignatureVersion = 'test'; AntivirusSignatureLastUpdated = $updated }
}
function Resolve-UnifiedDefenderExecutable { return 'C:\Windows\Trusted\MpCmdRun.exe' }
function Invoke-UnifiedDefenderCommand([string] $Executable, [string[]] $Argument, [int] $TimeoutMilliseconds) {
    $script:CommandCalls += 1
    if ($TimeoutMilliseconds -le 0 -or $TimeoutMilliseconds -gt 600000) { throw 'Invalid command deadline.' }
    if ($env:UNIFIED_TEST_CASE -eq 'update-error' -and $script:CommandCalls -eq 1) {
        return [pscustomobject]@{ exit_code = 2; stdout = ''; stderr = 'update failed' }
    }
    if ($script:CommandCalls -eq 1) {
        if (($Argument -join ',') -cne '-SignatureUpdate') { throw 'Expected a security-intelligence update first.' }
        return [pscustomobject]@{ exit_code = 0; stdout = 'up to date'; stderr = '' }
    }
    if ($Argument.Count -ne 6 -or $Argument[0] -cne '-Scan' -or $Argument[1] -cne '-ScanType' -or
        $Argument[2] -cne '3' -or $Argument[3] -cne '-File' -or
        $Argument[4] -cne $env:UNIFIED_TEST_ARTIFACT -or $Argument[5] -cne '-DisableRemediation') {
        throw 'Custom scan arguments changed or the literal artifact argument was split.'
    }
    if ($env:UNIFIED_TEST_CASE -eq 'scan-error') {
        return [pscustomobject]@{ exit_code = 2; stdout = ''; stderr = 'scan failed' }
    }
    if ($env:UNIFIED_TEST_CASE -eq 'detection') {
        return [pscustomobject]@{ exit_code = 2; stdout = 'Threat : TEST-DETECTION-NOT-MALWARE'; stderr = '' }
    }
    if ($env:UNIFIED_TEST_CASE -eq 'contradictory') {
        return [pscustomobject]@{ exit_code = 0; stdout = 'Threat : TEST-DETECTION-NOT-MALWARE'; stderr = '' }
    }
    if ($env:UNIFIED_TEST_CASE -eq 'timeout') { throw 'Simulated scan timeout.' }
    if ($env:UNIFIED_TEST_CASE -eq 'changed') { [IO.File]::AppendAllText($env:UNIFIED_TEST_ARTIFACT, 'changed') }
    if ($env:UNIFIED_TEST_CASE -eq 'removed') { [IO.File]::Delete($env:UNIFIED_TEST_ARTIFACT) }
    return [pscustomobject]@{ exit_code = 0; stdout = 'Scan completed'; stderr = '' }
}
$result = Invoke-UnifiedArtifactScan -Targets @($env:UNIFIED_TEST_ARTIFACT) -Output $env:UNIFIED_TEST_REPORT -BudgetSeconds 60
Write-Output ('RESULT:' + $result)
'''


@unittest.skipUnless(POWERSHELL, 'PowerShell 7 is required for the actual Defender scan decision tests')
class DefenderScanDecisionTests(unittest.TestCase):
    def scenario(self, name):
        with tempfile.TemporaryDirectory(prefix='Defender checks ') as temporary:
            root = Path(temporary).resolve()
            # Metacharacters and spaces stay one literal argument throughout;
            # no shell string, wildcard expansion, or executable artifact needed.
            artifact = root / 'Sample & release [1].bin'
            artifact.write_bytes(b'ordinary non-executable test artifact\n')
            report = root / 'result.json'
            harness = root / 'checks.ps1'
            harness.write_text(HARNESS, encoding='utf-8')
            env = dict(os.environ, UNIFIED_TEST_SCAN_SCRIPT=str(SCRIPT),
                       UNIFIED_TEST_CASE=name, UNIFIED_TEST_ARTIFACT=str(artifact),
                       UNIFIED_TEST_REPORT=str(report))
            result = subprocess.run([POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive',
                                     '-File', str(harness)], capture_output=True,
                                    text=True, encoding='utf-8', errors='replace',
                                    timeout=30, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(report.is_file(), result.stdout + result.stderr)
            parsed = json.loads(report.read_text(encoding='utf-8'))
            code_line = next(line for line in result.stdout.splitlines() if line.startswith('RESULT:'))
            return int(code_line.split(':', 1)[1]), parsed

    def test_clean_requires_current_active_scanner_and_unchanged_hashes(self):
        code, report = self.scenario('clean')
        self.assertEqual(code, 0)
        self.assertTrue(report['clean'])
        self.assertEqual(report['status'], 'clean')
        self.assertTrue(report['artifacts_unchanged'])
        self.assertEqual(len(report['artifact_inventory_before']), 1)
        self.assertEqual(report['artifact_inventory_before'], report['artifact_inventory_after'])
        self.assertEqual(len(report['artifact_inventory_before'][0]['sha256']), 64)
        self.assertEqual(len(report['scans']), 1)
        self.assertEqual(report['scans'][0]['exit_code'], 0)
        self.assertEqual(report['defender_after']['AMRunningMode'], 'Normal')

    def test_detection_errors_stale_or_disabled_protection_never_report_clean(self):
        for name in ('inactive', 'stopped', 'stale', 'update-error', 'scan-error',
                     'detection', 'contradictory', 'timeout', 'changed', 'removed'):
            with self.subTest(case=name):
                code, report = self.scenario(name)
                self.assertNotEqual(code, 0)
                self.assertFalse(report['clean'])
                self.assertNotEqual(report['status'], 'clean')
                self.assertTrue(report['reason'])
                if name in ('detection', 'contradictory'):
                    self.assertEqual(code, 2)
                    self.assertEqual(report['threat_names'], ['TEST-DETECTION-NOT-MALWARE'])
                    self.assertIn('TEST-DETECTION-NOT-MALWARE', report['scans'][0]['stdout'])
                if name == 'inactive':
                    self.assertEqual(report['status'], 'unavailable')
                    self.assertIsNone(report['signature_update'])


if __name__ == '__main__':
    unittest.main()
