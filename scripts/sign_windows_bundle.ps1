# Sign all PE files in an extracted AgentKit Studio Windows bundle using a
# Certum SimplySign cloud certificate.
#
# Usage (on the Windows machine with SimplySign Desktop logged in):
#   powershell -ExecutionPolicy Bypass -File sign_windows_bundle.ps1 `
#     -BundleDir "C:\path\to\AgentKitStudio" `
#     -Thumbprint "40HEXCHARS" `
#     [-SigntoolPath "C:\path\to\signtool.exe"]
#
# The thumbprint is the certificate's SHA1 fingerprint shown in SimplySign
# Desktop -> Manage certificates -> double-click the certificate.
# Timestamping uses Sectigo's RFC 3161 server per the Certum tutorial.
param(
    [Parameter(Mandatory = $true)][string]$BundleDir,
    [Parameter(Mandatory = $true)][string]$Thumbprint,
    [string]$SigntoolPath = ""
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path (Join-Path $BundleDir "AgentKitStudio.exe"))) {
    Write-Error "BundleDir does not contain AgentKitStudio.exe: $BundleDir"
    exit 1
}

# Locate signtool: explicit path > Certum Signtool.zip sibling > Windows SDK.
if ($SigntoolPath -eq "") {
    $candidates = @(
        (Join-Path $BundleDir "signtool.exe"),
        (Join-Path (Split-Path $BundleDir -Parent) "Signtool\signtool.exe"),
        (Join-Path (Split-Path $BundleDir -Parent) "signtool.exe")
    ) + (Get-ChildItem "C:\Program Files (x86)\Windows Kits\10\bin" -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty FullName)
    $found = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $found) {
        Write-Error @"
signtool.exe not found. Either:
  - download Certum's Signtool.zip (see the SimplySign tutorial) and extract it
    next to the bundle folder, or
  - install the Windows SDK, or
  - pass -SigntoolPath explicitly.
"@
        exit 1
    }
    $SigntoolPath = $found
}
Write-Host "Using signtool: $SigntoolPath"

# Collect every PE file in the bundle: the Electron exe, all DLLs, Python
# runtime, native .node/.pyd modules. Signing everything keeps Windows
# signature validation happy when the app verifies its own components.
$files = Get-ChildItem $BundleDir -Recurse -Include *.exe, *.dll, *.node, *.pyd |
    Where-Object { $_.FullName -notmatch '\\app\.asar' } |
    Select-Object -ExpandProperty FullName

Write-Host ("Signing {0} PE files..." -f $files.Count)
$failed = @()
foreach ($file in $files) {
    & $SigntoolPath sign /v /fd sha256 /sha1 $Thumbprint `
        /tr http://timestamp.sectigo.com /td sha256 `
        $file
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "FAILED: $file"
        $failed += $file
    }
}

if ($failed.Count -gt 0) {
    Write-Error ("{0} file(s) failed to sign:" -f $failed.Count)
    $failed | ForEach-Object { Write-Host "  $_" }
    exit 1
}

Write-Host ""
Write-Host "All files signed. Verifying..."
& $SigntoolPath verify /pa /v (Join-Path $BundleDir "AgentKitStudio.exe")
Write-Host "Done. Re-zip the bundle folder to produce the signed distribution."
