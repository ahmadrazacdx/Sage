<#
.SYNOPSIS
    Master build orchestrator for Sage installers.

.DESCRIPTION
    Builds a complete NSIS installer for the specified tier.
    Single entry point for both local and CI builds.

.PARAMETER Tier
    Build tier: fast, pro, fast-lite, pro-lite, or all.

.PARAMETER SkipFrontend
    Skip frontend rebuild (reuses existing dist/).

.PARAMETER SkipPython
    Skip Python runtime preparation (reuses cached staging).

.PARAMETER SkipDownload
    Skip artifact downloads (reuses cached artifacts).

.PARAMETER DryRun
    Stage everything but don't run NSIS compiler.

.EXAMPLE
    .\build.ps1 -Tier fast-lite          # Smallest build, fastest test
    .\build.ps1 -Tier fast               # CPU full build
    .\build.ps1 -Tier pro                # GPU full build
    .\build.ps1 -Tier all                # All 4 tiers
    .\build.ps1 -Tier fast -SkipFrontend # Reuse cached frontend
#>
param(
    [Parameter(Mandatory)]
    [ValidateSet('fast', 'pro', 'fast-lite', 'pro-lite', 'all')]
    [string]$Tier,

    [switch]$SkipFrontend,
    [switch]$SkipPython,
    [switch]$SkipDownload,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ProjectRoot = (Get-Item "$PSScriptRoot").FullName
$InstallerDir = Join-Path $ProjectRoot "installer"
$ScriptsDir = Join-Path $InstallerDir "scripts"
$CacheDir = Join-Path $InstallerDir ".cache"
$FrontendDir = Join-Path $ProjectRoot "frontend"
$FrontendApp = Join-Path $FrontendDir "artifacts/sage"
$SrcDir = Join-Path $ProjectRoot "src"
$LauncherDir = Join-Path $InstallerDir "launcher"
$LauncherExe = Join-Path $LauncherDir "Sage.exe"

$Manifest = Get-Content (Join-Path $InstallerDir "build-manifest.json") -Raw | ConvertFrom-Json
$Version = $Manifest.app_version

$Tiers = if ($Tier -eq 'all') { @('fast', 'pro', 'fast-lite', 'pro-lite') } else { @($Tier) }

# ---------- helpers ----------
function Write-Banner {
    param([string]$Msg)
    Write-Host ""
    Write-Host ("=" * 60) -ForegroundColor Magenta
    Write-Host "  $Msg" -ForegroundColor Magenta
    Write-Host ("=" * 60) -ForegroundColor Magenta
}
function Write-Step { param([string]$Msg) Write-Host "`n>>> $Msg" -ForegroundColor Cyan }
function Write-Ok { param([string]$Msg) Write-Host "    OK: $Msg" -ForegroundColor Green }
function Write-Warn { param([string]$Msg) Write-Host "    WARN: $Msg" -ForegroundColor Yellow }
function Assert-Command {
    param([string]$Cmd, [string]$Hint)
    if (-not (Get-Command $Cmd -ErrorAction SilentlyContinue)) {
        throw "Required command '$Cmd' not found. $Hint"
    }
}

function Ensure-MinGW {
    # 1. Already in PATH?
    if (Get-Command gcc -ErrorAction SilentlyContinue) {
        Write-Ok "gcc (MinGW) found in PATH"
        return
    }

    # 2. MSYS2 pre-install
    $msys2Gcc = "C:\msys64\mingw64\bin\gcc.exe"
    if (Test-Path $msys2Gcc) {
        $mingwBin = "C:\msys64\mingw64\bin"
        Write-Ok "MinGW found via MSYS2 at $mingwBin, adding to PATH"
        $env:PATH = $mingwBin + ";" + $env:PATH
        return
    }

    # 3. Winget fallback
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Warn "gcc not found; installing MinGW via winget (BrechtSanders.WinLibs.POSIX.UCRT)..."
        & winget install --id BrechtSanders.WinLibs.POSIX.UCRT --silent --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -eq 0) {
            $potentialPaths = @(
                "$env:LOCALAPPDATA\Programs\WinLibs",
                "$env:ProgramFiles\WinLibs"
            )
            foreach ($p in $potentialPaths) {
                if (Test-Path "$p\bin\gcc.exe") {
                    $env:PATH = "$p\bin;" + $env:PATH
                    Write-Ok "gcc (MinGW) installed via winget and added to PATH"
                    return
                }
            }
            Write-Warn "winget reported success but gcc.exe not found in expected paths. You may need to restart your terminal."
        }
    }

    # 4. Chocolatey fallback
    Write-Warn "gcc not found; trying Chocolatey..."
    if (Get-Command choco -ErrorAction SilentlyContinue) {
        & choco install mingw -y --no-progress
        if ($LASTEXITCODE -ne 0) { throw "Chocolatey failed to install MinGW." }

        $chocoMingw = "C:\ProgramData\chocolatey\lib\mingw\tools\install\mingw64\bin"
        if ((Test-Path $chocoMingw) -and ($env:PATH -notlike "*$chocoMingw*")) {
            $env:PATH = $chocoMingw + ";" + $env:PATH
        }

        if (Get-Command gcc -ErrorAction SilentlyContinue) {
            Write-Ok "gcc (MinGW) installed via Chocolatey"
            return
        }
    }

    throw "gcc (MinGW) not found. Please install it manually from https://winlibs.com/ or https://www.mingw-w64.org/ and add the 'bin' folder to your PATH."
}

# ---------- step 0: validate prerequisites ----------
Write-Banner "SAGE BUILD SYSTEM v$Version"
Write-Step "Validating prerequisites"

Assert-Command "uv"    "Install from https://docs.astral.sh/uv/"
Assert-Command "pnpm"  "Install from https://pnpm.io/"
Assert-Command "node"  "Install from https://nodejs.org/"
Ensure-MinGW

# Check NSIS (unless dry run)
$nsisExe = $null
if (-not $DryRun) {
    $nsisLocations = @(
        "${env:ProgramFiles(x86)}\NSIS\makensis.exe",
        "${env:ProgramFiles}\NSIS\makensis.exe"
    )
    $makensisPath = (Get-Command makensis -ErrorAction SilentlyContinue)
    if ($makensisPath) { $nsisLocations += $makensisPath.Source }

    foreach ($loc in $nsisLocations) {
        if ($loc -and (Test-Path $loc)) { $nsisExe = $loc; break }
    }
    if (-not $nsisExe) {
        throw "NSIS not found. Install from https://nsis.sourceforge.io/Download"
    }
    Write-Ok "NSIS: $nsisExe"
}

Write-Ok "All prerequisites satisfied"

# ---------- step 0.5: build launcher (C + MinGW) ----------
Write-Step "Building Windows launcher (C + MinGW)"

$iconPath = Join-Path $InstallerDir "sage.ico"
if (-not (Test-Path $iconPath)) {
    throw "sage.ico not found at $iconPath, the launcher requires it."
}

Push-Location $LauncherDir
try {
    Write-Host "    windres: compiling icon resource..."
    & windres launcher.rc -O coff -o launcher.res
    if ($LASTEXITCODE -ne 0) { throw "windres failed, could not compile launcher.rc" }

    Write-Host "    gcc: compiling and linking Sage.exe..."
    & gcc launcher.c launcher.res -o Sage.exe -mwindows -lshlwapi -Os -s "-Wl,--gc-sections"
    if ($LASTEXITCODE -ne 0) { throw "gcc failed - could not compile launcher.c" }

    Remove-Item launcher.res -Force -ErrorAction SilentlyContinue
}
finally {
    Pop-Location
}

if (-not (Test-Path $LauncherExe)) {
    throw "Launcher exe not found at $LauncherExe after build."
}

$launcherKB = [math]::Round((Get-Item $LauncherExe).Length / 1KB, 0)
Write-Ok "Launcher built: Sage.exe ($launcherKB KB)"

# ---------- step 1: build frontend ----------
if (-not $SkipFrontend) {
    Write-Step "Building frontend SPA"
    Push-Location $FrontendApp
    try {
        & pnpm install 2>&1 | Out-Null
        & pnpm build
        if ($LASTEXITCODE -ne 0) { throw "Frontend build failed" }
    }
    finally { Pop-Location }
    Write-Ok "Frontend built: $FrontendApp/dist"
}
else {
    Write-Ok "Frontend build skipped (--SkipFrontend)"
}

$FrontendDist = Join-Path $FrontendApp "dist"
if (-not (Test-Path $FrontendDist)) {
    throw "Frontend dist not found at $FrontendDist. Run without -SkipFrontend."
}

# ---------- step 2: build python wheel ----------
Write-Step "Building Python wheel"
Push-Location $ProjectRoot
try {
    & uv build --wheel --quiet --link-mode=copy
    if ($LASTEXITCODE -ne 0) { throw "Wheel build failed" }
}
finally { Pop-Location }

$WheelFile = Get-ChildItem (Join-Path $ProjectRoot "dist") -Filter "sage-*.whl" |
Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $WheelFile) { throw "No wheel found in dist/" }
Write-Ok "Wheel: $($WheelFile.Name)"

# ---------- build each tier ----------
foreach ($t in $Tiers) {
    Write-Banner "BUILDING TIER: $t (v$Version)"

    $StagingDir = Join-Path $InstallerDir "staging/$t"

    # Clean staging (except python if skipping)
    if (Test-Path $StagingDir) {
        if ($SkipPython -and (Test-Path "$StagingDir/python")) {
            Get-ChildItem $StagingDir -Exclude 'python' | Remove-Item -Recurse -Force
        }
        else {
            Remove-Item $StagingDir -Recurse -Force
        }
    }
    New-Item -ItemType Directory -Path $StagingDir -Force | Out-Null

    # --- 3: prepare python runtime ---
    if (-not $SkipPython) {
        Write-Step "Preparing Python runtime"
        & $ScriptsDir/prepare-python.ps1 `
            -StagingDir $StagingDir `
            -WheelPath $WheelFile.FullName `
            -CacheDir $CacheDir
        if ($LASTEXITCODE -ne 0) { throw "Python preparation failed" }
    }
    else {
        if (-not (Test-Path "$StagingDir/python/python.exe")) {
            throw "No cached Python runtime. Run without -SkipPython."
        }
        Write-Ok "Python runtime skipped (--SkipPython)"
    }

    # --- 4: download/stage external artifacts ---
    if (-not $SkipDownload) {
        Write-Step "Downloading artifacts for tier: $t"
        & $ScriptsDir/download-artifacts.ps1 `
            -Tier $t `
            -CacheDir $CacheDir `
            -OutputDir $StagingDir
        if ($LASTEXITCODE -ne 0) { throw "Artifact download failed" }
    }
    else {
        Write-Ok "Artifact download skipped (--SkipDownload)"
    }

    # --- 5: stage config, frontend, templates ---
    Write-Step "Staging application files"

    # Config
    $cfgDst = Join-Path $StagingDir "config"
    New-Item -ItemType Directory -Path $cfgDst -Force | Out-Null
    Copy-Item "$ProjectRoot/config/default.toml"      "$cfgDst/" -Force
    Copy-Item "$ProjectRoot/config/institution.toml"   "$cfgDst/" -Force
    if (Test-Path "$ProjectRoot/config/templates") {
        Copy-Item "$ProjectRoot/config/templates" "$cfgDst/templates" -Recurse -Force
    }

    # Launcher
    Copy-Item $LauncherExe (Join-Path $StagingDir "Sage.exe") -Force

    # Desktop icon (used by pywebview + taskbar/tray)
    $iconSrc = Join-Path $InstallerDir "sage.ico"
    if (Test-Path $iconSrc) {
        Copy-Item $iconSrc (Join-Path $StagingDir "sage.ico") -Force
    }
    else {
        Write-Warn "sage.ico not found. Desktop window/tray may use a default icon."
    }

    # Frontend dist
    $feDst = Join-Path $StagingDir "frontend/artifacts/sage/dist"
    New-Item -ItemType Directory -Path (Split-Path $feDst -Parent) -Force | Out-Null
    Copy-Item $FrontendDist $feDst -Recurse -Force
    if (-not (Test-Path "$feDst/favicon.ico")) {
        Write-Warn "favicon.ico not found in frontend dist. Installer will use default icon."
    }

    # Data directories (empty structure)
    $dataDirs = @(
        "artifacts/data/databases",
        "artifacts/data/exports",
        "artifacts/data/processed",
        "artifacts/data/raw",
        "artifacts/sandbox/data/sessions",
        "artifacts/sandbox/data/figures",
        "logs"
    )
    foreach ($dd in $dataDirs) {
        New-Item -ItemType Directory -Path (Join-Path $StagingDir $dd) -Force | Out-Null
    }

    Write-Ok "Application files staged"

    # --- 6: generate manifest.json ---
    Write-Step "Generating install manifest"

    $backend = if ($t -match 'pro') { 'cuda' } else { 'cpu' }
    $includesModels = $t -notmatch 'lite'

    $installManifest = @{
        version         = $Version
        tier            = $t
        platform        = "windows-x86_64"
        backend         = $backend
        includes_models = $includesModels
        build_timestamp = (Get-Date -Format 'o')
        files           = @{}
    }

    # Hash all files in staging using fast .NET methods
    $allFiles = Get-ChildItem $StagingDir -Recurse -File
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    foreach ($f in $allFiles) {
        $relPath = $f.FullName.Substring($StagingDir.Length + 1).Replace('\', '/')
        
        # Use .NET Stream for much faster hashing
        $stream = [System.IO.File]::OpenRead($f.FullName)
        $hashBytes = $sha256.ComputeHash($stream)
        $stream.Close()
        
        # Convert bytes to hex string
        $hash = [System.BitConverter]::ToString($hashBytes).Replace("-", "")

        $installManifest.files[$relPath] = @{
            sha256 = $hash
            size   = $f.Length
        }
    }
    $sha256.Dispose()

    $manifestPath = Join-Path $StagingDir "manifest.json"
    $installManifest | ConvertTo-Json -Depth 5 | Set-Content $manifestPath -Encoding UTF8
    Write-Ok "Manifest: $($allFiles.Count) files hashed"

    # --- 7: create payload archive ---
    $outDir = Join-Path $InstallerDir "output"
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null

    $baseName = "sage-$t-$Version-windows-x86_64"
    $payloadName = "$baseName.bin"
    $payloadPath = Join-Path $outDir $payloadName

    if (-not $DryRun) {
        Write-Step "Creating payload archive"
        Write-Host "    Packing python/ and artifacts/ -> $payloadName ..."
        Write-Host "    (This packs ~2-3 GB as a plain tar - may take 1-2 minutes)"
        Push-Location $StagingDir
        try {
            & tar -cf $payloadPath python artifacts
            if ($LASTEXITCODE -ne 0) { throw "tar failed to create payload archive" }
        }
        finally {
            Pop-Location
        }

        $payloadMB = [math]::Round((Get-Item $payloadPath).Length / 1MB, 0)
        Write-Ok "Payload: $payloadName ($payloadMB MB)"
    }

    # --- 8: compile NSIS installer stub ---
    if (-not $DryRun) {
        Write-Step "Compiling NSIS installer stub"

        $outName = "$baseName.exe"

        $nsisArgs = @(
            "/DTIER=$t",
            "/DVERSION=$Version",
            "/DSTAGING_DIR=$StagingDir",
            "/DOUTPUT_FILE=$outDir\$outName",
            "/DBACKEND=$backend",
            "/DPAYLOAD_NAME=$payloadName",
            (Join-Path $InstallerDir "sage.nsi")
        )

        Write-Host "    Running: makensis $($nsisArgs -join ' ')"
        & $nsisExe @nsisArgs

        if ($LASTEXITCODE -ne 0) { throw "NSIS compilation failed for tier $t" }

        $installerSize = [math]::Round((Get-Item "$outDir/$outName").Length / 1MB, 1)
        Write-Ok "Stub installer: $outName ($installerSize MB)"

        # SHA256 for the stub
        $installerHash = (Get-FileHash "$outDir/$outName" -Algorithm SHA256).Hash
        "$installerHash  $outName" | Set-Content "$outDir/SHA256SUMS.txt" -Encoding UTF8

        # SHA256 for the payload
        $payloadHash = (Get-FileHash $payloadPath -Algorithm SHA256).Hash
        "$payloadHash  $payloadName" | Add-Content "$outDir/SHA256SUMS.txt" -Encoding UTF8
        Write-Ok "SHA256SUMS.txt updated"

        # --- 9: create distribution zip ---
        Write-Step "Creating distribution package"
        $distZipName = "$baseName.zip"
        $distZipPath = Join-Path $outDir $distZipName

        Write-Host "    Bundling .exe + .bin into $distZipName ..."
        if (Test-Path $distZipPath) { Remove-Item $distZipPath -Force }

        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [System.IO.Compression.ZipFile]::Open($distZipPath, 'Create')
        try {
            $noComp = [System.IO.Compression.CompressionLevel]::NoCompression
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, "$outDir\$outName", $outName, $noComp) | Out-Null
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $payloadPath, $payloadName, $noComp) | Out-Null
        }
        finally {
            $zip.Dispose()
        }

        $distZipMB = [math]::Round((Get-Item $distZipPath).Length / 1MB, 0)
        Write-Ok "Distribution: $distZipName ($distZipMB MB)"
        Write-Host ""
        Write-Host "    Upload to Cloudflare R2:" -ForegroundColor Cyan
        Write-Host "      $distZipPath" -ForegroundColor White

    }
    else {
        Write-Ok "NSIS compilation skipped (--DryRun)"

        $totalMB = [math]::Round(($allFiles | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
        Write-Host "    Staging summary: $($allFiles.Count) files, $totalMB MB" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Banner "BUILD COMPLETE"
Write-Host "  Output: $InstallerDir/output/" -ForegroundColor Green
Write-Host ""
