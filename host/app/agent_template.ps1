#!/usr/bin/env pwsh
# Hermes Fleet node agent -- Windows/PowerShell version.
#
# Mirrors agent_template.sh's contract exactly (same register/heartbeat API,
# same "models are re-read from the real list every time" rule, same
# "bind Ollama to the tailnet IP only, never 0.0.0.0" rule) but every
# platform-specific step is native Windows: winget instead of apt/brew,
# CIM instead of /proc/meminfo, Invoke-RestMethod + ConvertTo-Json instead
# of hand-built curl JSON strings.
#
# Installed via:
#   irm 'http://<host-tailscale-ip>:8080/agent.ps1?token=...&model=...' | iex

$ErrorActionPreference = "Stop"

$HostUrl = "__HOST_URL__"
$Token   = "__TOKEN__"
$Model   = "__MODEL__"

$StateDir      = Join-Path $env:USERPROFILE ".hermes-fleet"
$NodeIdFile    = Join-Path $StateDir "node_id"
$LogFile       = Join-Path $StateDir "agent.log"
$HeartbeatFile = Join-Path $StateDir "heartbeat.ps1"
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

function Log($msg) { Write-Output "[hermes-fleet] $msg" }

# ---------------------------------------------------------------- 1. tailnet
# Same reasoning as the bash agent: this script only reached $HostUrl at all
# because it's already on the tailnet, and Ollama has no auth of its own --
# so it gets bound to this specific tailscale IP, never 0.0.0.0.
if (-not (Get-Command tailscale -ErrorAction SilentlyContinue)) {
    Log "ERROR: tailscale not found. Install it and run 'tailscale up' first."
    exit 1
}
$TsIp = (tailscale ip -4).Trim()
if (-not $TsIp) {
    Log "ERROR: could not determine this machine's Tailscale IP."
    exit 1
}
Log "tailscale IP: $TsIp"

# ------------------------------------------------------------ 2. node identity
if (Test-Path $NodeIdFile) {
    $NodeId = (Get-Content $NodeIdFile -Raw).Trim()
} else {
    $NodeId = [guid]::NewGuid().ToString("N")
    Set-Content -Path $NodeIdFile -Value $NodeId -NoNewline
}
Log "node id: $NodeId"

# ---------------------------------------------------------------- 3. ollama
# Same "safe to re-run" properties as the bash version: skip install if the
# binary exists, skip (re)binding if already reachable on the tailnet
# address, and `ollama pull` on an already-present model is a fast manifest
# check, not a re-download.
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Log "installing Ollama..."
    # Direct installer download, not winget -- winget/App Installer isn't
    # guaranteed present (confirmed missing on a real friend's machine
    # during testing). /VERYSILENT/NORESTART are standard Inno Setup flags
    # (Ollama's installer is Inno Setup-built) -- unverified against a real
    # Windows box, but the safe failure mode if wrong is just the installer
    # popping its normal GUI instead of running silently, not a hard error.
    $installer = Join-Path $env:TEMP "OllamaSetup.exe"
    Invoke-WebRequest -Uri "https://ollama.com/download/OllamaSetup.exe" -OutFile $installer -UseBasicParsing
    Start-Process -FilePath $installer -ArgumentList "/VERYSILENT", "/NORESTART" -Wait
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
} else {
    Log "Ollama already installed, skipping install"
}

function Test-OllamaReachable {
    try {
        Invoke-WebRequest -Uri "http://${TsIp}:11434/" -UseBasicParsing -TimeoutSec 2 | Out-Null
        return $true
    } catch {
        return $false
    }
}

if (Test-OllamaReachable) {
    Log "Ollama already reachable on ${TsIp}:11434, leaving it as-is"
} else {
    # The Windows installer starts Ollama as a background app bound to
    # 127.0.0.1 by default (there's no systemd-equivalent to reconfigure in
    # place here) -- stop that default instance so it isn't fighting a
    # second one over the same model storage, then start our own bound to
    # the tailnet address specifically.
    Get-Process -Name "ollama*" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1

    Log "starting Ollama on ${TsIp}:11434..."
    $env:OLLAMA_HOST = "${TsIp}:11434"
    Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden

    $ready = $false
    for ($i = 0; $i -lt 15; $i++) {
        if (Test-OllamaReachable) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) {
        Log "ERROR: Ollama did not become reachable on ${TsIp}:11434"
        exit 1
    }
}

Log "pulling $Model (already-present models are a fast check, not a re-download)..."
$env:OLLAMA_HOST = "${TsIp}:11434"
ollama pull $Model

# ------------------------------------------------------------ 4. GPU / VRAM
$Gpu = "CPU only"
$VramTotalMb = $null
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $gpuLine = (nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1)
    if ($gpuLine) {
        $parts = $gpuLine -split ","
        $Gpu = $parts[0].Trim()
        $VramTotalMb = [int]($parts[1].Trim())
    }
} else {
    # No reliable ROCm-equivalent VRAM query on Windows for non-NVIDIA GPUs,
    # and Win32_VideoController's AdapterRAM is well-documented as unreliable
    # (frequently wraps/caps around 4GB on many drivers) -- report the name
    # only, leave VRAM null rather than show a number known to be wrong.
    $videoController = Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($videoController) { $Gpu = $videoController.Name }
}

# --------------------------------------------------------- 5. OS / arch / RAM
$OsName = "Windows"
$Arch = $env:PROCESSOR_ARCHITECTURE
$RamTotalMb = [int]((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1MB)

# ------------------------------------------------------- 6. models this node has
# Same rule as the bash agent: read from `ollama list`, not just "the model
# we were told to pull" -- if the model set changes by hand later, the next
# registration has to catch up to reality.
$modelsOutput = (ollama list 2>$null)
$Models = @($modelsOutput | Select-Object -Skip 1 | ForEach-Object {
    ($_ -split '\s+')[0]
} | Where-Object { $_ })
if (-not $Models) { $Models = @($Model) }

function Register-Node {
    $body = @{
        token          = $Token
        node_id        = $NodeId
        name           = $env:COMPUTERNAME
        ip             = $TsIp
        port           = 11434
        backend        = "ollama"
        os             = $OsName
        arch           = $Arch
        gpu            = $Gpu
        vram_total_mb  = $VramTotalMb
        ram_total_mb   = $RamTotalMb
        agent_version  = "0.1.0"
        models         = $Models
    } | ConvertTo-Json

    Invoke-RestMethod -Uri "$HostUrl/api/register" -Method Post -ContentType "application/json" -Body $body
}

Log "registering with $HostUrl..."
$registerResp = Register-Node
Log "registered."

$HeartbeatInterval = if ($registerResp.heartbeat_interval_s) { $registerResp.heartbeat_interval_s } else { 5 }

# ------------------------------------------------------- 7. heartbeat forever
# Written to its own file and launched as a fully detached hidden process --
# same goal as the bash agent's `disown` + HUP trap: keep heartbeating after
# this window closes, without leaving a visible console around.
$heartbeatScript = @"
`$ErrorActionPreference = "SilentlyContinue"
`$HostUrl = "$HostUrl"
`$Token = "$Token"
`$NodeId = "$NodeId"
`$TsIp = "$TsIp"
`$Interval = $HeartbeatInterval

function Register-Node {
    `$body = @{
        token = `$Token; node_id = `$NodeId; name = `$env:COMPUTERNAME
        ip = `$TsIp; port = 11434; backend = "ollama"
    } | ConvertTo-Json
    Invoke-RestMethod -Uri "`$HostUrl/api/register" -Method Post -ContentType "application/json" -Body `$body
}

while (`$true) {
    `$vramUsed = `$null
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
        `$v = (nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>`$null | Select-Object -First 1)
        if (`$v) { `$vramUsed = [int]`$v.Trim() }
    }
    `$os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
    `$ramUsed = if (`$os) { [int]((`$os.TotalVisibleMemorySize - `$os.FreePhysicalMemory) / 1024) } else { `$null }

    `$body = @{
        token = `$Token; node_id = `$NodeId
        vram_used_mb = `$vramUsed; ram_used_mb = `$ramUsed
    } | ConvertTo-Json

    try {
        Invoke-RestMethod -Uri "`$HostUrl/api/heartbeat" -Method Post -ContentType "application/json" -Body `$body | Out-Null
    } catch {
        if (`$_.Exception.Response.StatusCode.value__ -eq 404) {
            # Host lost its DB -- re-register and carry on, same self-heal
            # path described in registry.py.
            try { Register-Node | Out-Null } catch {}
        }
    }
    Start-Sleep -Seconds `$Interval
}
"@
Set-Content -Path $HeartbeatFile -Value $heartbeatScript

Start-Process -FilePath "powershell" `
    -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", $HeartbeatFile `
    -WindowStyle Hidden `
    -RedirectStandardOutput $LogFile

Log "heartbeat loop running in the background, logging to $LogFile"
Log "done -- this node is now part of the fleet."
