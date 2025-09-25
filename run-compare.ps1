param(
  [switch]$NoInstall  # skip pip install on reruns
)

$ErrorActionPreference = "Stop"

function Is-StoreAlias([string]$path) {
  if (-not $path) { return $false }
  return ($path -like "*\WindowsApps\python.exe") -or ($path -like "*\WindowsApps\py.exe")
}

function Find-RealPython {
  # Try py launcher first
  $py = (Get-Command py -ErrorAction SilentlyContinue)
  if ($py -and -not (Is-StoreAlias $py.Path)) {
    return @{ exe="py"; isLauncher=$true }
  }

  # Try python on PATH but ignore WindowsApps alias
  $pyexe = (Get-Command python -ErrorAction SilentlyContinue)
  if ($pyexe -and -not (Is-StoreAlias $pyexe.Path)) {
    return @{ exe=$pyexe.Path; isLauncher=$false }
  }

  # Probe common install locations
  $roots = @()

  # Typical user install root
  $localRoot = Join-Path -Path $env:LOCALAPPDATA -ChildPath "Programs\Python"
  if (Test-Path $localRoot) { $roots += $localRoot }

  # Other common roots
  $roots += @(
    "C:\Program Files\Python311",
    "C:\Program Files\Python312",
    "C:\Python311",
    "C:\Python312"
  )

  foreach ($root in $roots) {
    if (-not (Test-Path $root)) { continue }
    $cand = Get-ChildItem -Path $root -Recurse -Filter python.exe -ErrorAction SilentlyContinue |
            Where-Object { -not (Is-StoreAlias $_.FullName) } |
            Select-Object -First 1 -ExpandProperty FullName
    if ($cand) {
      # Prepend to PATH for this session
      $env:Path = "{0};{1};{2}" -f (Split-Path $cand), (Join-Path (Split-Path $cand) "Scripts"), $env:Path
      return @{ exe=$cand; isLauncher=$false }
    }
  }

  return $null
}

Write-Host "Checking for a real Python interpreter..."
$pyInfo = Find-RealPython
if (-not $pyInfo) {
  Write-Warning @"
No real Python found.
Fix either of these and re-run:
  1) Install Python from https://www.python.org/downloads/ and check "Add python.exe to PATH", OR
  2) Disable the Microsoft Store alias: Settings → Apps → Advanced app settings → App execution aliases → turn OFF 'python' and 'python3'
"@
  throw "Python interpreter not found."
}

$exe = $pyInfo.exe
Write-Host "Using: $exe"

# Create venv
if (-not (Test-Path ".venv")) {
  if ($pyInfo.isLauncher) {
    & $exe -3 -m venv .venv
  } else {
    & $exe -m venv .venv
  }
}

# Activate (PowerShell)
$activate = ".\.venv\Scripts\Activate.ps1"
if (-not (Test-Path $activate)) {
  throw "Virtualenv activation script not found at $activate (venv creation failed)."
}

Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
. $activate

# Install deps
if (-not $NoInstall) {
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
}

if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env" -ErrorAction SilentlyContinue
  Write-Host "Created .env (copied from .env.example). Fill in your API keys if needed."
}

Write-Host "Starting app on http://127.0.0.1:5000"
python app.py
