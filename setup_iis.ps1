#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Sets up PII Redaction Pipeline on IIS with a PM2-supervised backend.

.DESCRIPTION
    1. Checks URL Rewrite and ARR modules are installed
    2. Enables ARR server-level proxy
    3. Installs waitress + pywin32 into the virtual environment
    4. Creates an IIS site (default port 80)
    5. Installs PM2 + pm2-windows-service, registers PM2 as a Windows Service,
       and starts the app under PM2 (ecosystem.config.js) on port 5009
    6. Opens Windows Firewall for port 80

.NOTES
    Run this script from an Administrator PowerShell window.
    IIS must already be installed (assumed present).
    Node.js (with npm) must already be installed — https://nodejs.org/.
    Prerequisites to install manually BEFORE running this script if not already present:
      - IIS URL Rewrite  : https://www.iis.net/downloads/microsoft/url-rewrite
      - IIS ARR 3.0      : https://www.iis.net/downloads/microsoft/application-request-routing

    Process supervision was previously provided by windows_service.py (a pywin32
    Windows Service). That path is retired as of the PM2 migration — see
    wiki/feature-updates/phase-1/plan-pm2-implementation.md, Open Question #1:
    windows_service.py had no configured crash-recovery (Windows Service Recovery
    actions were never set) and defaulted to Manual startup unless changed by hand,
    so it did not reliably deliver what it claimed to. windows_service.py itself is
    left in the repo for reference but is no longer installed or started by this
    script.
#>

$ErrorActionPreference = "Stop"
$ProjectRoot     = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython      = Join-Path $ProjectRoot "pii_env\Scripts\python.exe"
$EcosystemFile   = Join-Path $ProjectRoot "ecosystem.config.js"
$SiteName        = "PIIRedaction"
$SitePort        = 5009
$SiteIP          = "172.16.29.61"
$Pm2ServiceName  = "PM2"

function Write-Step { param([string]$msg) Write-Host "`n>>> $msg" -ForegroundColor Cyan }
function Write-OK   { param([string]$msg) Write-Host "    [OK]  $msg" -ForegroundColor Green }
function Write-Warn { param([string]$msg) Write-Host "    [!]   $msg" -ForegroundColor Yellow }
function Write-Fail { param([string]$msg) Write-Host "    [ERR] $msg" -ForegroundColor Red }

# ── 0. Validate project layout ────────────────────────────────────────────────
Write-Step "Validating project files..."
if (-not (Test-Path $VenvPython)) {
    Write-Fail "Virtual environment not found: $VenvPython"
    Write-Host "    Create it first:" -ForegroundColor Red
    Write-Host "      cd `"$ProjectRoot`"" -ForegroundColor Yellow
    Write-Host "      python -m venv pii_env" -ForegroundColor Yellow
    Write-Host "      pii_env\Scripts\python.exe -m pip install -r src\requirements.txt" -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path $EcosystemFile)) {
    Write-Fail "ecosystem.config.js not found in project root."
    exit 1
}
Write-OK "Project files look good."

# ── 1. IIS assumed present ────────────────────────────────────────────────────
Write-Step "IIS already installed — skipping IIS setup."
Write-OK "Using existing IIS installation."

# ── 2. Check URL Rewrite ──────────────────────────────────────────────────────
Write-Step "Checking URL Rewrite module..."
$rewriteDll = "C:\Windows\System32\inetsrv\rewrite.dll"
if (-not (Test-Path $rewriteDll)) {
    Write-Fail "IIS URL Rewrite module is NOT installed."
    Write-Host ""
    Write-Host "  Download and install from:" -ForegroundColor Yellow
    Write-Host "  https://www.iis.net/downloads/microsoft/url-rewrite" -ForegroundColor Yellow
    Write-Host "  Then re-run this script." -ForegroundColor Yellow
    exit 1
}
Write-OK "URL Rewrite module found."

# ── 3. Check ARR ─────────────────────────────────────────────────────────────
Write-Step "Checking Application Request Routing (ARR)..."
$arrDll = "C:\Windows\System32\inetsrv\arr.dll"
if (-not (Test-Path $arrDll)) {
    Write-Fail "IIS ARR 3.0 is NOT installed."
    Write-Host ""
    Write-Host "  Download and install from:" -ForegroundColor Yellow
    Write-Host "  https://www.iis.net/downloads/microsoft/application-request-routing" -ForegroundColor Yellow
    Write-Host "  Then re-run this script." -ForegroundColor Yellow
    exit 1
}
Write-OK "ARR module found."

# ── 4. Enable ARR proxy at server level ──────────────────────────────────────
Write-Step "Enabling ARR server-level proxy..."
try {
    Import-Module WebAdministration -ErrorAction Stop
    Set-WebConfigurationProperty -pspath "MACHINE/WEBROOT/APPHOST" `
        -filter "system.webServer/proxy" -name "enabled" -value $true
    Write-OK "ARR proxy enabled."
} catch {
    Write-Warn "Could not set ARR proxy via PowerShell: $_"
    Write-Warn "Enable it manually: IIS Manager → server node → ARR → Server Proxy Settings → Enable proxy."
}

# ── 5. Install Python packages ────────────────────────────────────────────────
Write-Step "Installing waitress and pywin32 into virtual environment..."
& $VenvPython -m pip install waitress pywin32 --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Fail "pip install failed."
    exit 1
}
Write-OK "waitress + pywin32 installed."

# Run pywin32 post-install hook
$postInstall = Join-Path $ProjectRoot "pii_env\Scripts\pywin32_postinstall.py"
if (Test-Path $postInstall) {
    Write-Host "    Running pywin32 post-install..." -ForegroundColor Gray
    & $VenvPython $postInstall -install 2>&1 | Out-Null
}

# ── 6. Create IIS site ────────────────────────────────────────────────────────
Write-Step "Creating IIS site '$SiteName' on port $SitePort..."
Import-Module WebAdministration -ErrorAction Stop

if (Get-Website -Name $SiteName -ErrorAction SilentlyContinue) {
    Write-Warn "Site '$SiteName' already exists — removing to recreate..."
    Remove-Website -Name $SiteName
}

New-Website -Name $SiteName `
            -Port $SitePort `
            -IPAddress $SiteIP `
            -PhysicalPath $ProjectRoot `
            -ApplicationPool "DefaultAppPool" | Out-Null

# Verify web.config is present
$webConfigPath = Join-Path $ProjectRoot "web.config"
if (Test-Path $webConfigPath) {
    Write-OK "web.config found — reverse proxy rules will be active."
} else {
    Write-Warn "web.config not found in project root — proxy rules missing!"
}

Write-OK "IIS site '$SiteName' created on port $SitePort."

# ── 7. Install PM2 and start the app under supervision ───────────────────────
Write-Step "Checking Node.js / npm..."
$nodeCmd = Get-Command node -ErrorAction SilentlyContinue
$npmCmd  = Get-Command npm  -ErrorAction SilentlyContinue
if (-not $nodeCmd -or -not $npmCmd) {
    Write-Fail "Node.js/npm not found on PATH — required to run PM2."
    Write-Host "  Install Node.js LTS from https://nodejs.org/ then re-run this script." -ForegroundColor Yellow
    exit 1
}
Write-OK "Node.js $(node -v) / npm $(npm -v) found."

Write-Warn "Before continuing: if a previous ad hoc 'python serve.py' (or any other"
Write-Warn "process) is already bound to port $SitePort on this machine, stop it now —"
Write-Warn "PM2 starting a second copy while the old one is still listening will leave"
Write-Warn "two processes competing for the same port. Check with:"
Write-Warn "  netstat -ano | findstr :$SitePort"

Write-Step "Installing PM2 and pm2-windows-service (global npm packages)..."
npm install -g pm2 pm2-windows-service --silent
if ($LASTEXITCODE -ne 0) {
    Write-Fail "npm install failed."
    exit 1
}
Write-OK "pm2 + pm2-windows-service installed."

Write-Step "Registering PM2 as a Windows Service ('$Pm2ServiceName')..."
$pm2Svc = Get-Service -Name $Pm2ServiceName -ErrorAction SilentlyContinue
if ($pm2Svc) {
    Write-OK "'$Pm2ServiceName' service already registered — skipping install."
} else {
    Write-Warn "pm2-service-install may prompt for PM2_HOME and a couple of other"
    Write-Warn "settings — accept the defaults unless you have a specific reason not to."
    pm2-service-install
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "pm2-service-install failed — see output above."
        exit 1
    }
}

$pm2Svc = Get-Service -Name $Pm2ServiceName -ErrorAction SilentlyContinue
if ($pm2Svc) {
    Set-Service -Name $Pm2ServiceName -StartupType Automatic
}

Write-Step "Starting the app under PM2 (ecosystem.config.js)..."
Push-Location $ProjectRoot
pm2 start ecosystem.config.js
if ($LASTEXITCODE -ne 0) {
    Pop-Location
    Write-Fail "pm2 start failed — see output above."
    exit 1
}
pm2 save
Pop-Location
Write-OK "App started under PM2 and process list saved — will resurrect on reboot via the '$Pm2ServiceName' service."

Write-Step "Installing pm2-logrotate (caps PM2's own log file size/count)..."
pm2 install pm2-logrotate | Out-Null
pm2 set pm2-logrotate:max_size 10M | Out-Null
pm2 set pm2-logrotate:retain 14 | Out-Null
pm2 set pm2-logrotate:compress true | Out-Null
Write-OK "pm2-logrotate installed (10MB per file, 14 rotations retained, compressed)."

# ── 8. Firewall ───────────────────────────────────────────────────────────────
Write-Step "Configuring Windows Firewall for port $SitePort..."
$fwRule = Get-NetFirewallRule -DisplayName "PII Redaction Pipeline $SitePort" -ErrorAction SilentlyContinue
if (-not $fwRule) {
    New-NetFirewallRule -DisplayName "PII Redaction Pipeline $SitePort" `
                        -Direction Inbound -Protocol TCP `
                        -LocalPort $SitePort -Action Allow | Out-Null
    Write-OK "Firewall rule added for port $SitePort."
} else {
    Write-OK "Firewall rule already exists for port $SitePort."
}

# ── Done ──────────────────────────────────────────────────────────────────────
$hostname = $env:COMPUTERNAME
Write-Host ""
Write-Host "==========================================================" -ForegroundColor Green
Write-Host "  PII Redaction Pipeline is live!" -ForegroundColor Green
Write-Host ""
Write-Host "  URL        : http://$SiteIP`:$SitePort" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Backend    : waitress on 127.0.0.1:5009  (supervised by PM2)" -ForegroundColor Gray
Write-Host "  Frontend   : IIS site '$SiteName' on port $SitePort" -ForegroundColor Gray
Write-Host ""
Write-Host "  App management (PM2):" -ForegroundColor Gray
Write-Host "    Status  : pm2 status" -ForegroundColor Gray
Write-Host "    Logs    : pm2 logs pii-redaction" -ForegroundColor Gray
Write-Host "    Restart : pm2 restart pii-redaction" -ForegroundColor Gray
Write-Host "    Stop    : pm2 stop pii-redaction" -ForegroundColor Gray
Write-Host ""
Write-Host "  PM2 daemon Windows Service ('$Pm2ServiceName') management:" -ForegroundColor Gray
Write-Host "    Start   : Start-Service $Pm2ServiceName" -ForegroundColor Gray
Write-Host "    Stop    : Stop-Service $Pm2ServiceName" -ForegroundColor Gray
Write-Host "    Restart : Restart-Service $Pm2ServiceName" -ForegroundColor Gray
Write-Host "==========================================================" -ForegroundColor Green
