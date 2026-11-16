param(
    [string]$Hosts = "auto",
    [ValidateSet("copy", "link")][string]$Mode = "copy",
    [string]$Project = "",
    [switch]$WithDeps,
    [switch]$CheckDeps
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

function Find-Python38 {
    foreach ($cmd in @("python", "python3", "py")) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if (-not $found) { continue }
        try {
            & $cmd -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { return $cmd }
        } catch { }
    }
    foreach ($p in (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue | Sort-Object FullName -Descending)) {
        return $p.FullName
    }
    throw "Python 3.8+ not found on PATH."
}

$Py = Find-Python38
Write-Host "== Installing /write-paper v2 using $Py ==" -ForegroundColor Cyan

if ($WithDeps) {
    & $Py -m pip install -r (Join-Path $RepoRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "pip install failed." }
}

$InstallArgs = @((Join-Path $RepoRoot "install.py"), "--hosts", $Hosts, "--mode", $Mode)
if ($Project) { $InstallArgs += @("--project", $Project) }
# The dependency report is cheap and always printed (same as install.sh); -CheckDeps is kept for compatibility.
$InstallArgs += "--check-deps"

& $Py @InstallArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Done. Use '/write-paper' in Claude Code, Antigravity, Codex, OpenCode, or run scripts/run_paper.py." -ForegroundColor Green
