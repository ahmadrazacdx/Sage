<#
.SYNOPSIS
    Computes SHA256 hashes for all local artifacts.
.DESCRIPTION
    Prints SHA256 hashes for models and binaries in artifacts/.
    Use these to fill FILL_AFTER_FIRST_DOWNLOAD placeholders in build-manifest.json.
#>
$ProjectRoot = (Get-Item "$PSScriptRoot/../../").FullName

Write-Host "=== SHA256 Hash Calculator ===" -ForegroundColor Cyan

$files = @(
    "artifacts/models/Qwen3.5-2B-Q4_K_M.gguf",
    "artifacts/models/Qwen3.5-4B-Q4_K_M.gguf",
    "artifacts/models/Qwen3.5-0.8B-Q4_K_M.gguf",
    "artifacts/typst/typst.exe",
    "artifacts/mmdr/mmdr.exe"
)

foreach ($f in $files) {
    $path = Join-Path $ProjectRoot $f
    if (Test-Path $path) {
        $hash = (Get-FileHash $path -Algorithm SHA256).Hash
        $sizeMB = [math]::Round((Get-Item $path).Length / 1MB, 1)
        Write-Host "$f`n  SHA256: $hash  ($sizeMB MB)" -ForegroundColor Green
    } else {
        Write-Host "$f  -- NOT FOUND" -ForegroundColor Red
    }
}

$cacheDir = Join-Path $ProjectRoot "installer/.cache"
if (Test-Path $cacheDir) {
    Write-Host "`n--- Cached Downloads ---" -ForegroundColor Yellow
    Get-ChildItem $cacheDir -File | ForEach-Object {
        $hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash
        Write-Host "$($_.Name)`n  SHA256: $hash" -ForegroundColor Green
    }
}
