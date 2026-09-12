# Record local, copy after. Do not point OBS at the NAS / TVA_ROOT.
# Usage:
#   powershell -File copy-after-obs.ps1 -Source "D:\OBS" -Root "T:\tradevid"
# One-shot: copies stable *.mp4|*.mkv into $TVA_ROOT\recordings\ then ingest.

param(
    [Parameter(Mandatory = $true)]
    [string] $Source,

    [string] $Root = $env:TVA_ROOT
)

if (-not $Root) {
    Write-Error "TVA_ROOT is unset. Pass -Root or setx TVA_ROOT T:\tradevid"
    exit 1
}

$env:TVA_ROOT = $Root
& tva --root $Root watch --source $Source --once
exit $LASTEXITCODE
