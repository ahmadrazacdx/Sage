<#
.SYNOPSIS
    Prepares a portable, stripped Python runtime for Sage installers.

.DESCRIPTION
    Downloads python-build-standalone, installs the Sage wheel + deps
    via uv (which uses its global cach), then strips
    aggressively to minimize size.

.PARAMETER StagingDir
    Output directory for the prepared Python runtime.

.PARAMETER WheelPath
    Path to the built sage .whl file.

.PARAMETER CacheDir
    Directory for caching downloads (default: installer/.cache).
#>
param(
    [Parameter(Mandatory)][string]$StagingDir,
    [Parameter(Mandatory)][string]$WheelPath,
    [string]$CacheDir = "$PSScriptRoot/../.cache"
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# ---------- helpers ----------
function Write-Step { param([string]$Msg) Write-Host "`n>>> $Msg" -ForegroundColor Cyan }
function Write-Ok { param([string]$Msg) Write-Host "    OK: $Msg" -ForegroundColor Green }
function Write-Warn { param([string]$Msg) Write-Host "    WARN: $Msg" -ForegroundColor Yellow }

# ---------- read manifest ----------
$Manifest = Get-Content "$PSScriptRoot/../build-manifest.json" -Raw | ConvertFrom-Json
$PyInfo = $Manifest.python_standalone
$PyUrl = $PyInfo.source
$PyHash = $PyInfo.sha256
$PyArchive = Join-Path $CacheDir "python-standalone.tar.gz"

New-Item -ItemType Directory -Path $CacheDir  -Force | Out-Null
New-Item -ItemType Directory -Path $StagingDir -Force | Out-Null

$PythonDir = Join-Path $StagingDir "python"

# ---------- step 1: download python-build-standalone ----------
Write-Step "Downloading Python standalone runtime"

if (Test-Path $PyArchive) {
    Write-Ok "Cached at $PyArchive"
}
else {
    Write-Host "    Downloading from $PyUrl ..."
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $PyUrl -OutFile $PyArchive -UseBasicParsing
    $ProgressPreference = 'Continue'
    Write-Ok "Downloaded ($([math]::Round((Get-Item $PyArchive).Length / 1MB, 1)) MB)"
}

# SHA256 verify (skip if placeholder)
if ($PyHash -and $PyHash -ne "FILL_AFTER_FIRST_DOWNLOAD") {
    $actual = (Get-FileHash $PyArchive -Algorithm SHA256).Hash
    if ($actual -ne $PyHash) {
        Remove-Item $PyArchive -Force
        throw "SHA256 mismatch for Python archive!`nExpected: $PyHash`nActual:   $actual"
    }
    Write-Ok "SHA256 verified"
}
else {
    $actual = (Get-FileHash $PyArchive -Algorithm SHA256).Hash
    Write-Warn "SHA256 not pinned yet. Current hash: $actual"
}

# ---------- step 2: extract ----------
Write-Step "Extracting Python runtime"

if (Test-Path $PythonDir) {
    Remove-Item $PythonDir -Recurse -Force
}

# tar.gz extraction
tar -xzf $PyArchive -C $StagingDir 2>$null
if (-not (Test-Path "$PythonDir/python.exe")) {
    # Some archives have a nested directory
    $nested = Get-ChildItem $StagingDir -Directory | Where-Object { Test-Path "$($_.FullName)/python.exe" } | Select-Object -First 1
    if ($nested) {
        Rename-Item $nested.FullName $PythonDir
    }
    else {
        throw "Could not find python.exe after extraction in $StagingDir"
    }
}
Write-Ok "Extracted to $PythonDir"

# ---------- step 3: install sage + deps ----------
Write-Step "Installing Sage wheel + dependencies into portable Python"

$PythonExe = Join-Path $PythonDir "python.exe"

# Ensure pip is available
& $PythonExe -m ensurepip --upgrade
& $PythonExe -m pip install --upgrade pip --quiet --no-warn-script-location

# Install the wheel with all deps. Try offline-first using uv cache.
$uvPath = Get-Command uv -ErrorAction SilentlyContinue
if ($uvPath) {
    Write-Host "    Using uv for installation (local-first)..."
    $offlineSuccess = $true
    & uv pip install $WheelPath --python $PythonExe --quiet --link-mode=copy --offline
    if ($LASTEXITCODE -ne 0) {
        $offlineSuccess = $false
    }
    if (-not $offlineSuccess) {
        Write-Warn "Offline installation failed (missing cached packages). Attempting to fetch from PyPI..."
        & uv pip install $WheelPath --python $PythonExe --quiet --link-mode=copy
    }
}
else {
    Write-Host "    Using pip for installation..."
    & $PythonExe -m pip install $WheelPath --quiet
}

if ($LASTEXITCODE -ne 0) { throw "Failed to install Sage wheel" }
Write-Ok "Sage + dependencies installed"

# ---------- step 4: strip aggressively ----------
Write-Step "Stripping Python runtime for minimal size"

$SitePackages = Join-Path $PythonDir "Lib/site-packages"
$StdLib = Join-Path $PythonDir "Lib"
$beforeSize = (Get-ChildItem $PythonDir -Recurse -File | Measure-Object -Property Length -Sum).Sum

# 4a. Remove test directories everywhere
$testDirs = Get-ChildItem $SitePackages -Recurse -Directory |
Where-Object { $_.Name -in @('tests', 'test', 'testing', '_tests') }
foreach ($d in $testDirs) {
    Remove-Item $d.FullName -Recurse -Force -ErrorAction SilentlyContinue
}

# 4b. Remove docs, examples, benchmarks
$docDirs = Get-ChildItem $SitePackages -Recurse -Directory |
Where-Object { $_.Name -in @('docs', 'doc', 'examples', 'example', 'benchmarks', 'benchmark', 'samples') }
foreach ($d in $docDirs) {
    Remove-Item $d.FullName -Recurse -Force -ErrorAction SilentlyContinue
}

# 4c. Remove __pycache__ (we'll recompile)
Get-ChildItem $PythonDir -Recurse -Directory -Filter '__pycache__' |
ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }

# 4d. Partial .dist-info prune (keep METADATA and RECORD for pip)
Get-ChildItem $SitePackages -Directory -Filter '*.dist-info' | ForEach-Object {
    Get-ChildItem $_.FullName -File | Where-Object {
        $_.Name -notin @('METADATA', 'RECORD', 'INSTALLER', 'WHEEL', 'top_level.txt', 'entry_points.txt')
    } | Remove-Item -Force -ErrorAction SilentlyContinue
}

# 4e. Remove .pdb debug files
Get-ChildItem $PythonDir -Recurse -Filter '*.pdb' |
Remove-Item -Force -ErrorAction SilentlyContinue

# 4f. Remove .pyi stub files (not needed at runtime)
Get-ChildItem $SitePackages -Recurse -Filter '*.pyi' |
Remove-Item -Force -ErrorAction SilentlyContinue

# 4g. Remove dev-only packages that shouldn't be in production
$devPackages = @('pip', 'setuptools', '_distutils_hack', 'pkg_resources')
foreach ($pkg in $devPackages) {
    $pkgDir = Join-Path $SitePackages $pkg
    if (Test-Path $pkgDir) {
        Remove-Item $pkgDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    # Also remove dist-info
    Get-ChildItem $SitePackages -Directory -Filter "$pkg-*.dist-info" |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}
# Remove pip exe
Get-ChildItem (Join-Path $PythonDir "Scripts") -Filter 'pip*' -ErrorAction SilentlyContinue |
Remove-Item -Force -ErrorAction SilentlyContinue

# 4h. Remove stdlib modules not needed at runtime
$stdlibRemove = @('ensurepip', 'idlelib', 'tkinter', 'turtledemo',
    'lib2to3', 'pydoc_data', 'unittest/test', 'test')
foreach ($mod in $stdlibRemove) {
    $modPath = Join-Path $StdLib $mod
    if (Test-Path $modPath) {
        Remove-Item $modPath -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# 4i. Remove tcl/tk (not needed — pywebview uses WebView2)
$tclDir = Join-Path $PythonDir "tcl"
if (Test-Path $tclDir) { Remove-Item $tclDir -Recurse -Force }
Get-ChildItem $PythonDir -Filter 'tcl*.dll' | Remove-Item -Force -ErrorAction SilentlyContinue
Get-ChildItem $PythonDir -Filter 'tk*.dll'  | Remove-Item -Force -ErrorAction SilentlyContinue
Get-ChildItem $PythonDir -Filter '_tkinter*' | Remove-Item -Force -ErrorAction SilentlyContinue

# ---------- step 5: compile .pyc ----------
Write-Step "Compiling Python bytecode"
& $PythonExe -m compileall -q -b $SitePackages

$afterSize = (Get-ChildItem $PythonDir -Recurse -File | Measure-Object -Property Length -Sum).Sum
$savedMB = [math]::Round(($beforeSize - $afterSize) / 1MB, 1)
$finalMB = [math]::Round($afterSize / 1MB, 1)

Write-Ok "Stripped $savedMB MB -> final size: $finalMB MB"
Write-Host ""
