# Liber installer: installs the /write-paper skill into every agent found on this machine.
#
#   One line (PowerShell):
#     irm https://raw.githubusercontent.com/Misaka16384/liber/master/install.ps1 | iex
#   One line with options:
#     & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Misaka16384/liber/master/install.ps1))) -Hosts claude -WithDeps
#   From a clone:
#     .\install.ps1 [-Hosts claude,antigravity] [-Mode link] [-Project <dir>] [-WithDeps]
param(
    [string[]]$Hosts = @("auto"),  # array, so that an unquoted `-Hosts claude,antigravity` works
    [ValidateSet("copy", "link")][string]$Mode = "copy",
    [string]$Project = "",
    [switch]$WithDeps,
    [switch]$CheckDeps,
    [string]$Ref = "master",
    [string]$Repo = "https://github.com/Misaka16384/liber"
)

function Install-Liber {
    param([string]$Hosts, [string]$Mode, [string]$Project, [bool]$WithDeps, [string]$Ref, [string]$Repo, [string]$ScriptRoot)

    # Local to this function: `irm | iex` runs in the caller's session, which must not be altered.
    $ErrorActionPreference = "Stop"

    function Find-Python38 {
        if ($env:WRITE_PAPER_PYTHON) { return $env:WRITE_PAPER_PYTHON }
        foreach ($cmd in @("python", "python3", "py")) {
            if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { continue }
            try {
                & $cmd -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" 2>$null
                if ($LASTEXITCODE -eq 0) { return $cmd }
            } catch { }
        }
        foreach ($p in (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue | Sort-Object FullName -Descending)) {
            return $p.FullName
        }
        throw "Python 3.8+ not found (install it, or set WRITE_PAPER_PYTHON)."
    }

    $Py = Find-Python38
    $TempDir = $null
    $RepoRoot = $ScriptRoot
    try {
        if (-not $RepoRoot -or -not (Test-Path (Join-Path $RepoRoot "skills\write-paper\SKILL.md"))) {
            # Run through `irm | iex`: download the repository into a temporary folder first.
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            $TempDir = Join-Path ([IO.Path]::GetTempPath()) ("liber-" + [guid]::NewGuid().ToString("N"))
            New-Item -ItemType Directory -Path $TempDir | Out-Null
            $zip = Join-Path $TempDir "liber.zip"
            Write-Host "== Downloading Liber ($Ref) from $Repo ==" -ForegroundColor Cyan
            Invoke-WebRequest -UseBasicParsing -Uri "$Repo/archive/refs/heads/$Ref.zip" -OutFile $zip
            Expand-Archive -Path $zip -DestinationPath $TempDir -Force
            $RepoRoot = (Get-ChildItem $TempDir -Directory | Where-Object { Test-Path (Join-Path $_.FullName "install.py") } | Select-Object -First 1).FullName
            if (-not $RepoRoot) { throw "Downloaded archive does not contain install.py." }
            if ($Mode -eq "link") {
                Write-Host "Note: -Mode link needs a permanent clone; installing copies instead." -ForegroundColor Yellow
                $Mode = "copy"
            }
        }

        Write-Host "== Installing the /write-paper skill (Liber) using $Py ==" -ForegroundColor Cyan
        if ($WithDeps) {
            & $Py -m pip install -r (Join-Path $RepoRoot "requirements.txt")
            if ($LASTEXITCODE -ne 0) { throw "pip install failed." }
        }

        $InstallArgs = @((Join-Path $RepoRoot "install.py"), "--hosts", $Hosts, "--mode", $Mode, "--check-deps")
        if ($Project) { $InstallArgs += @("--project", $Project) }
        & $Py @InstallArgs
        # `throw`, never `exit`: under `irm | iex`, exit would close the user's PowerShell window.
        if ($LASTEXITCODE -ne 0) { throw "install.py failed with exit code $LASTEXITCODE." }
        Write-Host "Done. Start a new agent session and use /write-paper (Claude Code plugin users: /liber:write-paper)." -ForegroundColor Green
    }
    finally {
        if ($TempDir -and (Test-Path $TempDir)) { Remove-Item -Recurse -Force $TempDir -ErrorAction SilentlyContinue }
    }
}

# -CheckDeps is accepted for compatibility; the dependency report is always printed.
Install-Liber -Hosts ($Hosts -join ",") -Mode $Mode -Project $Project -WithDeps ([bool]$WithDeps) -Ref $Ref -Repo $Repo -ScriptRoot $PSScriptRoot
