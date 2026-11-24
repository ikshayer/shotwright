[CmdletBinding()]
param(
    [switch]$Offline,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AdditionalArguments
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$useOffline = $Offline
if ($AdditionalArguments) {
    if ($AdditionalArguments.Count -ne 1 -or $AdditionalArguments[0] -ne "--offline") {
        throw "Unsupported argument(s): $($AdditionalArguments -join ', '); use -Offline or --offline."
    }
    $useOffline = $true
}

$crateDirectory = Join-Path $PSScriptRoot "ntsc-pipe"
$targetDirectory = Join-Path $crateDirectory "target"
$releaseBinary = Join-Path $targetDirectory "release\ntsc-pipe.exe"
$localBinDirectory = Join-Path $PSScriptRoot "bin"
$installedBinary = Join-Path $localBinDirectory "ntsc-pipe.exe"
$hashFile = Join-Path $localBinDirectory "ntsc-pipe.exe.sha256"

if (-not (Test-Path -LiteralPath $crateDirectory -PathType Container)) {
    throw "ntsc-pipe source directory is missing: $crateDirectory"
}

$cargoArgs = @("build", "--release", "--locked", "--target-dir", $targetDirectory)
if ($useOffline) {
    $cargoArgs += "--offline"
}

Push-Location -LiteralPath $crateDirectory
try {
    & cargo @cargoArgs
    if ($LASTEXITCODE -ne 0) {
        throw "cargo $($cargoArgs -join ' ') failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath $releaseBinary -PathType Leaf)) {
    throw "cargo reported success but the release binary is missing: $releaseBinary"
}

$null = New-Item -ItemType Directory -Path $localBinDirectory -Force
Copy-Item -LiteralPath $releaseBinary -Destination $installedBinary -Force
$digest = (Get-FileHash -LiteralPath $installedBinary -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText(
    $hashFile,
    "sha256:$digest`n",
    [System.Text.UTF8Encoding]::new($false)
)

Write-Output "Installed: $installedBinary"
Write-Output "SHA-256: sha256:$digest"
Write-Output "Pin the digest in PostProfile.identity.pinned_binaries['ntsc-pipe']."
