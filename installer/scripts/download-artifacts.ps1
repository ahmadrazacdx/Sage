<#
.SYNOPSIS
    Stages all external artifacts for a Sage build tier.

.DESCRIPTION
    Reads build-manifest.json and stages models/binaries needed for the
    specified tier into the OutputDir.

    Resolution order (local-first):
      1. Project artifacts/ : already present on dev machine -> copy directly
      2. installer/.cache/  : previously downloaded archive   -> extract & copy
      3. Remote URL         : download, verify SHA256, cache, extract & copy

    This means a dev machine with all artifacts already in artifacts/ will
    never make a network request. CI with a warm cache also stays offline.

.PARAMETER Tier
    Build tier: fast, pro, fast-lite, pro-lite

.PARAMETER CacheDir
    Download cache directory (default: installer/.cache)

.PARAMETER OutputDir
    Where to place extracted artifacts (default: installer/staging/<tier>)
#>
param(
    [Parameter(Mandatory)]
    [ValidateSet('fast','pro','fast-lite','pro-lite')]
    [string]$Tier,

    [string]$CacheDir  = "$PSScriptRoot/../.cache",
    [string]$OutputDir = ""
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $OutputDir) { $OutputDir = "$PSScriptRoot/../staging/$Tier" }

$ProjectRoot = (Get-Item "$PSScriptRoot/../../").FullName

function Write-Step { param([string]$Msg) Write-Host "`n>>> $Msg" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Msg) Write-Host "    OK: $Msg" -ForegroundColor Green }
function Write-Warn { param([string]$Msg) Write-Host "    WARN: $Msg" -ForegroundColor Yellow }
function Write-Info { param([string]$Msg) Write-Host "    $Msg" }

function Assert-SHA256 {
    param([string]$Path, [string]$Expected)
    if (-not $Expected -or $Expected -in @("FILL_AFTER_FIRST_DOWNLOAD", "SKIP_DIRECTORY")) {
        $actual = (Get-FileHash $Path -Algorithm SHA256).Hash
        Write-Warn "SHA256 not pinned. Actual: $actual"
        return
    }
    $actual = (Get-FileHash $Path -Algorithm SHA256).Hash
    if ($actual -ne $Expected.ToUpper()) {
        throw "SHA256 MISMATCH for $Path`nExpected: $Expected`nActual:   $actual"
    }
    Write-Ok "SHA256 verified: $([System.IO.Path]::GetFileName($Path))"
}

function Download-File {
    param([string]$Url, [string]$OutFile)
    $tmpFile = "$OutFile.downloading"
    try {
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -Uri $Url -OutFile $tmpFile -UseBasicParsing
        $ProgressPreference = 'Continue'
        Move-Item $tmpFile $OutFile -Force
    } catch {
        if (Test-Path $tmpFile) { Remove-Item $tmpFile -Force }
        throw
    }
}

function Extract-Archive {
    param([string]$ArchivePath, [string]$DestDir)
    New-Item -ItemType Directory -Path $DestDir -Force | Out-Null
    if ($ArchivePath -match '\.zip$') {
        Expand-Archive -Path $ArchivePath -DestinationPath $DestDir -Force
    } elseif ($ArchivePath -match '\.(tar\.gz|tgz)$') {
        tar -xzf $ArchivePath -C $DestDir
    } else {
        throw "Unsupported archive format: $ArchivePath"
    }
    # If all files are inside a single nested subdirectory, unwrap it
    $children = Get-ChildItem $DestDir
    if ($children.Count -eq 1 -and $children[0].PSIsContainer) {
        return $children[0].FullName
    }
    return $DestDir
}

# ---------- load manifest ----------
$Manifest = Get-Content "$PSScriptRoot/../build-manifest.json" -Raw | ConvertFrom-Json

New-Item -ItemType Directory -Path $CacheDir  -Force | Out-Null
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

# ---------- process models ----------
Write-Step "Processing models for tier: $Tier"

foreach ($prop in $Manifest.models.PSObject.Properties) {
    $model = $prop.Value
    $tiers = @($model.tiers)
    if ($Tier -notin $tiers) {
        Write-Info "SKIP: $($model.filename) (not in $Tier)"
        continue
    }

    $installPath = Join-Path $OutputDir $model.install_path
    $destDir     = Split-Path $installPath -Parent
    New-Item -ItemType Directory -Path $destDir -Force | Out-Null

    # --- Determine local source path (local_src overrides install_path for HF layout) ---
    $srcRelPath = if ($model.PSObject.Properties['local_src'] -and $model.local_src) {
        $model.local_src
    } else {
        $model.install_path
    }
    $localSrc = Join-Path $ProjectRoot $srcRelPath

    # 1. Local project artifacts (always preferred)
    if (Test-Path $localSrc) {
        Copy-Item $localSrc $installPath -Recurse -Force
        Write-Ok "From local artifacts: $($model.filename)"
        continue
    }

    # source == "local" but file missing → warn and skip
    if ($model.source -eq "local") {
        Write-Warn "Local source not found: $localSrc"
        continue
    }

    # 2. Cached download
    $cacheFile = Join-Path $CacheDir $model.filename
    if (Test-Path $cacheFile) {
        Write-Ok "From cache: $($model.filename)"
    } else {
        # 3. Remote download
        Write-Info "Downloading $($model.filename) ..."
        Download-File -Url $model.source -OutFile $cacheFile
        $sizeMB = [math]::Round((Get-Item $cacheFile).Length / 1MB, 1)
        Write-Ok "Downloaded: $($model.filename) ($sizeMB MB)"
    }

    Assert-SHA256 -Path $cacheFile -Expected $model.sha256
    Copy-Item $cacheFile $installPath -Force
    Write-Ok "Staged: $($model.install_path)"
}

# ---------- process binaries ----------
Write-Step "Processing binaries for tier: $Tier"

foreach ($prop in $Manifest.binaries.PSObject.Properties) {
    $bin    = $prop.Value
    $tiers  = @($bin.tiers)
    if ($Tier -notin $tiers) {
        Write-Info "SKIP: $($prop.Name) (not in $Tier)"
        continue
    }

    $installDir   = Join-Path $OutputDir $bin.install_path
    $localSrcDir  = Join-Path $ProjectRoot $bin.install_path
    New-Item -ItemType Directory -Path $installDir -Force | Out-Null

    $copiedFromLocal = $false

    # 1. Local project artifacts (always preferred — dev machine has everything)
    if (Test-Path $localSrcDir) {
    $localFiles = @(Get-ChildItem $localSrcDir -File -ErrorAction SilentlyContinue)
        if ($localFiles.Count -gt 0) {
            Copy-Item "$localSrcDir\*" $installDir -Recurse -Force
            Write-Ok "From local artifacts: $($prop.Name) ($($localFiles.Count) files)"
            $copiedFromLocal = $true
        }
    }

    if (-not $copiedFromLocal) {
        if ($bin.source -eq "local") {
            Write-Warn "Local source not found: $localSrcDir"
        } else {
            # 2. Cached download
            $archiveName = [System.IO.Path]::GetFileName($bin.source)
            $cacheFile   = Join-Path $CacheDir $archiveName

            if (Test-Path $cacheFile) {
                Write-Ok "From cache: $archiveName"
            } else {
                # 3. Remote download
                Write-Info "Downloading $archiveName ..."
                Download-File -Url $bin.source -OutFile $cacheFile
                Write-Ok "Downloaded: $archiveName"
            }

            Assert-SHA256 -Path $cacheFile -Expected $bin.sha256

            $tmpExtract = Join-Path $CacheDir "_extract_$($prop.Name)"
            if (Test-Path $tmpExtract) { Remove-Item $tmpExtract -Recurse -Force }
            $extractedDir = Extract-Archive -ArchivePath $cacheFile -DestDir $tmpExtract
            Copy-Item "$extractedDir\*" $installDir -Recurse -Force
            Remove-Item $tmpExtract -Recurse -Force
        }
    }

    # Strip unwanted executables (applies regardless of source)
    if ($bin.PSObject.Properties['strip_patterns'] -and $bin.strip_patterns) {
        $stripped = 0
        foreach ($pattern in $bin.strip_patterns) {
            Get-ChildItem $installDir -Filter $pattern -File -ErrorAction SilentlyContinue |
                ForEach-Object { Remove-Item $_.FullName -Force; $stripped++ }
        }
        if ($stripped -gt 0) {
            Write-Ok "Stripped $stripped unwanted executables from $($prop.Name)"
        }
    }

    $fileCount = (Get-ChildItem $installDir -File -ErrorAction SilentlyContinue | Measure-Object).Count
    $dirSizeMB = [math]::Round((Get-ChildItem $installDir -Recurse -File -ErrorAction SilentlyContinue |
        Measure-Object -Property Length -Sum).Sum / 1MB, 1)
    Write-Ok "Staged: $($bin.install_path) ($fileCount files, $dirSizeMB MB)"
}

Write-Host "`n=== Artifact staging complete for tier: $Tier ===" -ForegroundColor Green
