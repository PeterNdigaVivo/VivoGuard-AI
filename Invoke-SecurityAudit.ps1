<#
.SYNOPSIS
    Read-only Windows security audit. Collects hygiene, malware-persistence,
    network-exposure, and account/secret indicators into a single report.

.DESCRIPTION
    Makes NO changes to the system - every command is inspection-only.
    Run in an elevated PowerShell session (Run as Administrator) for full
    results; it will still run without admin but some sections will be empty.

.USAGE
    1. Save this file (e.g. to your Desktop).
    2. Open PowerShell as Administrator.
    3. If scripts are blocked, allow this one for the current session only:
         Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
    4. Run it:
         .\Invoke-SecurityAudit.ps1
    5. Share the generated report file (path is printed at the end).
#>

[CmdletBinding()]
param(
    [string]$OutFile = "$env:USERPROFILE\Desktop\SecurityAudit_$(Get-Date -Format 'yyyyMMdd_HHmmss').txt"
)

$ErrorActionPreference = 'SilentlyContinue'

# --- helpers ---------------------------------------------------------------
function Write-Section {
    param([string]$Title)
    @(
        ''
        ('=' * 70)
        "  $Title"
        ('=' * 70)
    ) | Tee-Object -FilePath $OutFile -Append | Out-Host
}

function Capture {
    param(
        [string]$Label,
        [scriptblock]$Command
    )
    "`n--- $Label ---" | Tee-Object -FilePath $OutFile -Append | Out-Host
    try {
        $result = & $Command | Out-String
        if ([string]::IsNullOrWhiteSpace($result)) { $result = "(no data / nothing found)" }
    }
    catch {
        $result = "ERROR: $($_.Exception.Message)"
    }
    $result.TrimEnd() | Tee-Object -FilePath $OutFile -Append | Out-Host
}

# --- header ----------------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

"Windows Security Audit"            | Set-Content -Path $OutFile
"Generated : $(Get-Date)"           | Add-Content -Path $OutFile
"Host      : $env:COMPUTERNAME"     | Add-Content -Path $OutFile
"User      : $env:USERNAME"         | Add-Content -Path $OutFile
"Elevated  : $isAdmin"              | Add-Content -Path $OutFile
if (-not $isAdmin) {
    "WARNING   : Not running as Administrator - some sections will be incomplete." |
        Add-Content -Path $OutFile
}

# === 1. GENERAL HYGIENE ====================================================
Write-Section "1. GENERAL HYGIENE - updates, encryption, firewall, AV"

Capture "OS / hardware" {
    Get-ComputerInfo | Select-Object OsName, OsVersion, OsBuildNumber,
        WindowsProductName, CsManufacturer, CsModel
}
Capture "Recent updates (last 15)" {
    Get-HotFix | Sort-Object InstalledOn -Descending |
        Select-Object -First 15 HotFixID, Description, InstalledOn
}
Capture "BitLocker disk encryption" {
    Get-BitLockerVolume | Select-Object MountPoint, VolumeStatus,
        ProtectionStatus, EncryptionPercentage
}
Capture "Defender / AV status" {
    Get-MpComputerStatus | Select-Object AMRunningMode, AntivirusEnabled,
        RealTimeProtectionEnabled, AntispywareSignatureLastUpdated, QuickScanEndTime
}
Capture "Defender past threat detections" {
    Get-MpThreatDetection | Select-Object ThreatID, InitialDetectionTime, Resources
}

# === 2. MALWARE / COMPROMISE INDICATORS ====================================
Write-Section "2. MALWARE / COMPROMISE INDICATORS"

Capture "Startup / autorun entries" {
    Get-CimInstance Win32_StartupCommand |
        Select-Object Name, Command, Location, User
}
Capture "Non-Microsoft scheduled tasks (enabled)" {
    Get-ScheduledTask | Where-Object {
        $_.TaskPath -notlike '\Microsoft\*' -and $_.State -ne 'Disabled'
    } | Select-Object TaskName, TaskPath, State
}
Capture "Running non-Microsoft services" {
    Get-CimInstance Win32_Service | Where-Object {
        $_.State -eq 'Running' -and
        $_.PathName -notlike '*Microsoft*' -and
        $_.PathName -notlike '*Windows*'
    } | Select-Object Name, StartName, PathName
}
Capture "Top 20 processes by memory" {
    Get-Process | Sort-Object WS -Descending |
        Select-Object -First 20 Name, Id, Path
}
Capture "Failed logons (event 4625, last 24h)" {
    Get-WinEvent -FilterHashtable @{
        LogName='Security'; Id=4625; StartTime=(Get-Date).AddDays(-1)
    } | Select-Object -First 20 TimeCreated,
        @{N='Account';E={$_.Properties[5].Value}},
        @{N='Source';E={$_.Properties[19].Value}}
}

# === 3. NETWORK EXPOSURE ===================================================
Write-Section "3. NETWORK EXPOSURE - ports & sharing"

Capture "Firewall profiles (all should be Enabled=True)" {
    Get-NetFirewallProfile | Select-Object Name, Enabled
}
Capture "Listening TCP ports + owning process" {
    Get-NetTCPConnection -State Listen |
        Select-Object LocalAddress, LocalPort, OwningProcess,
            @{N='Process';E={(Get-Process -Id $_.OwningProcess).Name}} |
        Sort-Object LocalPort
}
Capture "Established outbound connections" {
    Get-NetTCPConnection -State Established |
        Select-Object LocalPort, RemoteAddress, RemotePort,
            @{N='Process';E={(Get-Process -Id $_.OwningProcess).Name}} |
        Sort-Object RemoteAddress
}
Capture "SMB file/printer shares" {
    Get-SmbShare | Select-Object Name, Path, Description
}
Capture "Remote Desktop (0 = RDP ENABLED)" {
    [PSCustomObject]@{
        fDenyTSConnections = (Get-ItemProperty `
            'HKLM:\System\CurrentControlSet\Control\Terminal Server').fDenyTSConnections
    }
}

# === 4. ACCOUNTS & SECRETS =================================================
Write-Section "4. ACCOUNTS & SECRETS"

Capture "Local user accounts" {
    Get-LocalUser | Select-Object Name, Enabled, LastLogon,
        PasswordLastSet, PasswordExpires
}
Capture "Members of local Administrators group" {
    Get-LocalGroupMember -Group 'Administrators' |
        Select-Object Name, PrincipalSource
}
Capture "Stored Windows credentials (names only)" {
    cmdkey /list
}
Capture "SSH keys in user profile" {
    Get-ChildItem "$env:USERPROFILE\.ssh" |
        Select-Object Name, Length, LastWriteTime
}

# --- footer ----------------------------------------------------------------
Write-Section "AUDIT COMPLETE"
"Report written to: $OutFile" | Tee-Object -FilePath $OutFile -Append | Out-Host
Write-Host "`nDone. Share the file above for interpretation." -ForegroundColor Green
