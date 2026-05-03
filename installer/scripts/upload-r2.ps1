<#
.SYNOPSIS
    Uploads Sage installer to Cloudflare R2.

.DESCRIPTION
    Uses AWS CLI (S3-compatible) to upload installer and manifest to R2.
    Immutable uploads — never overwrites existing versions.

    Required environment variables (set as GitHub Secrets):
      R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME

.PARAMETER Tier
    Build tier to upload.

.PARAMETER Version
    Version string (e.g. 0.1.0).

.PARAMETER InstallerPath
    Path to the .exe installer file.
#>
param(
    [Parameter(Mandatory)][string]$Tier,
    [Parameter(Mandatory)][string]$Version,
    [Parameter(Mandatory)][string]$InstallerPath
)

$ErrorActionPreference = 'Stop'

# Validate env vars
$requiredVars = @('R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_BUCKET_NAME')
foreach ($v in $requiredVars) {
    if (-not [Environment]::GetEnvironmentVariable($v)) {
        throw "Missing environment variable: $v"
    }
}

$AccountId = $env:R2_ACCOUNT_ID
$Bucket    = $env:R2_BUCKET_NAME
$Endpoint  = "https://$AccountId.r2.cloudflarestorage.com"

# Configure AWS CLI for R2
$env:AWS_ACCESS_KEY_ID     = $env:R2_ACCESS_KEY_ID
$env:AWS_SECRET_ACCESS_KEY = $env:R2_SECRET_ACCESS_KEY
$env:AWS_DEFAULT_REGION    = "auto"

$FileName  = [System.IO.Path]::GetFileName($InstallerPath)
$S3Key     = "v$Version/$FileName"
$S3Uri     = "s3://$Bucket/$S3Key"

# Check if version already exists (immutable)
$exists = aws s3 ls $S3Uri --endpoint-url $Endpoint 2>$null
if ($exists) {
    throw "Version v$Version/$FileName already exists in R2. Builds are immutable."
}

# Upload installer
Write-Host "Uploading $FileName to R2..."
aws s3 cp $InstallerPath $S3Uri --endpoint-url $Endpoint
Write-Host "OK: Uploaded $S3Key"

# Upload SHA256
$hashFile = Join-Path (Split-Path $InstallerPath) "SHA256SUMS.txt"
if (Test-Path $hashFile) {
    aws s3 cp $hashFile "s3://$Bucket/v$Version/SHA256SUMS.txt" --endpoint-url $Endpoint
    Write-Host "OK: Uploaded SHA256SUMS.txt"
}

# Upload manifest
$manifestFile = Join-Path (Split-Path $InstallerPath) "../staging/$Tier/manifest.json"
if (Test-Path $manifestFile) {
    aws s3 cp $manifestFile "s3://$Bucket/v$Version/manifest-$Tier.json" --endpoint-url $Endpoint
    Write-Host "OK: Uploaded manifest-$Tier.json"
}

# Update latest.json pointer
$latest = @{
    latest_version = $Version
    updated_at     = (Get-Date -Format 'o')
    tiers          = @{
        $Tier = @{
            installer = $S3Key
            size_bytes = (Get-Item $InstallerPath).Length
        }
    }
} | ConvertTo-Json -Depth 3

$latestFile = Join-Path ([System.IO.Path]::GetTempPath()) "latest.json"

# Merge with existing latest.json if present
$existingLatest = $null
try {
    aws s3 cp "s3://$Bucket/latest.json" $latestFile --endpoint-url $Endpoint 2>$null
    $existingLatest = Get-Content $latestFile -Raw | ConvertFrom-Json
} catch {}

if ($existingLatest -and $existingLatest.latest_version -eq $Version) {
    $existingLatest.tiers | Add-Member -NotePropertyName $Tier -NotePropertyValue @{
        installer  = $S3Key
        size_bytes = (Get-Item $InstallerPath).Length
    } -Force
    $existingLatest | ConvertTo-Json -Depth 3 | Set-Content $latestFile -Encoding UTF8
} else {
    $latest | Set-Content $latestFile -Encoding UTF8
}

aws s3 cp $latestFile "s3://$Bucket/latest.json" --endpoint-url $Endpoint
Write-Host "OK: Updated latest.json"
Remove-Item $latestFile -Force -ErrorAction SilentlyContinue

Write-Host "`n=== Upload complete: $S3Key ===" -ForegroundColor Green
