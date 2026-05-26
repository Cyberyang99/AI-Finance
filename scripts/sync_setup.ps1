# AI-Finance two-machine sync via OneDrive symlinks.
#
# Design:
#   git-tracked stuff (framework/, sectors.yaml, README) stays in place.
#   .gitignore'd private data (cot/, theses/user/, situations/*.md,
#     agent.db, episodic/) is moved to OneDrive\AI-Finance-data\.
#     Original locations become symlinks/junctions.
#
# Usage:
#   First machine (has the data):
#     powershell -File scripts\sync_setup.ps1 -Mode push
#   Second machine (after git clone):
#     powershell -File scripts\sync_setup.ps1 -Mode pull
#   Inspect state without changes:
#     powershell -File scripts\sync_setup.ps1 -Mode status
#
# Notes:
#   Directories use Junction (no admin needed).
#   Files use SymbolicLink, falling back to HardLink if not elevated.
#   HardLink keeps both paths pointing to the same inode on the same volume (NTFS C:).
#   Pass -DryRun to preview without touching files.

param(
    [Parameter(Mandatory=$true)][ValidateSet("push", "pull", "status")]
    [string]$Mode,
    [string]$ProjectRoot = "C:\Users\cyberyang99\projects\AI-Finance",
    [string]$CloudRoot   = "$env:OneDrive\AI-Finance-data",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# Subpaths inside memory/ to sync. Add/remove here.
$SyncItems = @(
    "agent.db",
    "knowledge\cot",
    "theses\user",
    "episodic",
    "situations"
)

function Write-Step($msg) { Write-Host "[$Mode] $msg" -ForegroundColor Cyan }
function Write-Skip($msg) { Write-Host "[$Mode] $msg" -ForegroundColor DarkGray }
function Write-Done($msg) { Write-Host "[$Mode] $msg" -ForegroundColor Green }

$memDir   = Join-Path $ProjectRoot "memory"
$cloudMem = Join-Path $CloudRoot "memory"

if (-not (Test-Path $memDir)) {
    throw "memory/ not found: $memDir"
}

if ($Mode -eq "status") {
    Write-Step "project memory: $memDir"
    Write-Step "cloud target:   $cloudMem"
    if (-not (Test-Path $cloudMem)) { Write-Skip "cloud not initialized yet" }
    foreach ($item in $SyncItems) {
        $local = Join-Path $memDir $item
        $cloud = Join-Path $cloudMem $item
        $isLink = $false
        if (Test-Path $local) {
            $i = Get-Item $local -Force
            $isLink = ($i.LinkType -eq "SymbolicLink") -or ($i.LinkType -eq "Junction")
        }
        $localState = if (-not (Test-Path $local)) { "MISSING" }
                      elseif ($isLink) { "SYMLINK" }
                      else { "LOCAL" }
        $cloudState = if (Test-Path $cloud) { "YES" } else { "NO" }
        Write-Host ("  {0,-22} local:{1,-10} cloud:{2}" -f $item, $localState, $cloudState)
    }
    return
}

if ($Mode -eq "push") {
    Write-Step "PUSH: move private data to OneDrive, replace with symlinks"

    if (-not (Test-Path $cloudMem)) {
        if ($DryRun) { Write-Skip "[dry-run] would create $cloudMem" }
        else {
            New-Item -ItemType Directory -Path $cloudMem -Force | Out-Null
            Write-Done "created $cloudMem"
        }
    }

    foreach ($item in $SyncItems) {
        $local = Join-Path $memDir $item
        $cloud = Join-Path $cloudMem $item

        if (Test-Path $local) {
            $i = Get-Item $local -Force
            if ($i.LinkType -in @("SymbolicLink", "Junction")) {
                Write-Skip "$item already a symlink, skip"
                continue
            }
        }

        if (Test-Path $local) {
            if (Test-Path $cloud) {
                # Both exist - assume cloud is canonical (e.g. previous partial run).
                # Remove local entity to allow link creation below.
                Write-Step "$item exists in both local and cloud; treating cloud as canonical, removing local copy"
                if ($DryRun) {
                    Write-Skip "[dry-run] skip remove local"
                } else {
                    if (Test-Path $local -PathType Container) {
                        Remove-Item $local -Recurse -Force
                    } else {
                        Remove-Item $local -Force
                    }
                }
            } else {
                Write-Step "moving $item -> cloud"
                if ($DryRun) {
                    Write-Skip "[dry-run] skip move"
                } else {
                    $parent = Split-Path $cloud -Parent
                    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
                    Move-Item -Path $local -Destination $cloud -Force
                }
            }
        } else {
            if (-not (Test-Path $cloud)) {
                Write-Skip "$item missing both locally and in cloud, skip"
                continue
            }
        }

        $linkParent = Split-Path $local -Parent
        if (-not (Test-Path $linkParent)) { New-Item -ItemType Directory -Path $linkParent -Force | Out-Null }
        $isDir = (Test-Path $cloud -PathType Container)
        Write-Step "linking: $local -> $cloud"
        if ($DryRun) {
            Write-Skip "[dry-run] skip symlink"
        } else {
            if ($isDir) {
                New-Item -ItemType Junction -Path $local -Target $cloud | Out-Null
                Write-Done "$item OK (junction)"
            } else {
                try {
                    New-Item -ItemType SymbolicLink -Path $local -Target $cloud -ErrorAction Stop | Out-Null
                    Write-Done "$item OK (symlink)"
                } catch [System.UnauthorizedAccessException] {
                    Write-Step "symlink needs admin; falling back to hardlink"
                    New-Item -ItemType HardLink -Path $local -Target $cloud | Out-Null
                    Write-Done "$item OK (hardlink)"
                } catch {
                    if ($_.Exception.Message -match "Administrator|privilege|elevat") {
                        Write-Step "symlink needs admin; falling back to hardlink"
                        New-Item -ItemType HardLink -Path $local -Target $cloud | Out-Null
                        Write-Done "$item OK (hardlink)"
                    } else { throw }
                }
            }
        }
    }
    Write-Done "PUSH complete. On the second machine, run -Mode pull"
    return
}

if ($Mode -eq "pull") {
    Write-Step "PULL: replace local memory subdirs with links to OneDrive"

    if (-not (Test-Path $cloudMem)) {
        throw "cloud $cloudMem does not exist. Wait for OneDrive sync, or run -Mode push on the source machine first."
    }

    foreach ($item in $SyncItems) {
        $local = Join-Path $memDir $item
        $cloud = Join-Path $cloudMem $item

        if (-not (Test-Path $cloud)) {
            Write-Skip "$item not in cloud, skip"
            continue
        }

        if (Test-Path $local) {
            $i = Get-Item $local -Force
            if ($i.LinkType -in @("SymbolicLink", "Junction")) {
                Write-Skip "$item already a symlink, skip"
                continue
            }
            Write-Step "$item has local content; backing up to ${item}.bak.local"
            if ($DryRun) {
                Write-Skip "[dry-run] skip backup/replace"
                continue
            }
            Move-Item -Path $local -Destination "$local.bak.local" -Force
        }

        $linkParent = Split-Path $local -Parent
        if (-not (Test-Path $linkParent)) { New-Item -ItemType Directory -Path $linkParent -Force | Out-Null }
        $isDir = (Test-Path $cloud -PathType Container)
        Write-Step "linking: $local -> $cloud"
        if ($DryRun) { Write-Skip "[dry-run] skip symlink" }
        else {
            if ($isDir) {
                New-Item -ItemType Junction -Path $local -Target $cloud | Out-Null
                Write-Done "$item OK (junction)"
            } else {
                try {
                    New-Item -ItemType SymbolicLink -Path $local -Target $cloud -ErrorAction Stop | Out-Null
                    Write-Done "$item OK (symlink)"
                } catch [System.UnauthorizedAccessException] {
                    Write-Step "symlink needs admin; falling back to hardlink"
                    New-Item -ItemType HardLink -Path $local -Target $cloud | Out-Null
                    Write-Done "$item OK (hardlink)"
                } catch {
                    if ($_.Exception.Message -match "Administrator|privilege|elevat") {
                        Write-Step "symlink needs admin; falling back to hardlink"
                        New-Item -ItemType HardLink -Path $local -Target $cloud | Out-Null
                        Write-Done "$item OK (hardlink)"
                    } else { throw }
                }
            }
        }
    }
    Write-Done "PULL complete. Both machines now share OneDrive data"
    return
}
