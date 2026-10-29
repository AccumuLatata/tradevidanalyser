# Record local, copy after. Do not point OBS at the NAS / TVA_ROOT.
# Usage:
#   powershell -File copy-after-obs.ps1 -Source "D:\OBS" -Root $env:TVA_ROOT
# Desk example (D8): -Root "Z:\tradevid". Never pass a UNC NAS path.
# One-shot: copies stable *.mp4|*.mkv into $TVA_ROOT\recordings\ then ingest.
# With TVA_SIDECAR=1 also copies <stem>.jsonl next to the recording (not .events.jsonl).

param(
    [Parameter(Mandatory = $true)]
    [string] $Source,

    [string] $Root = $env:TVA_ROOT
)

if (-not $Root) {
    Write-Error "TVA_ROOT is unset. Pass -Root or set TVA_ROOT (setx TVA_ROOT <path>)."
    exit 1
}

$env:TVA_ROOT = $Root
& tva --root $Root watch --source $Source --once
exit $LASTEXITCODE
