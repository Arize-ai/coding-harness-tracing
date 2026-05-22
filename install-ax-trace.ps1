# Installer for ax-trace on Windows.
#
# Usage:
#   irm https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install-ax-trace.ps1 | iex

$ErrorActionPreference = "Stop"

$Repo = "Arize-ai/coding-harness-tracing"
$InstallDir = if ($env:AX_TRACE_INSTALL_DIR) { $env:AX_TRACE_INSTALL_DIR } else { "$env:LOCALAPPDATA\Programs\ax-trace" }
$Version = $env:AX_TRACE_VERSION

# Fail fast on unsupported architectures (release pipeline only builds windows_amd64).
if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") {
    throw "Unsupported architecture: ARM64. ax-trace currently ships only windows_amd64."
}

if (-not $Version) {
    # The repo ships non-ax-trace releases too, so paginate until we find an ax-trace-v* tag.
    $tag = $null
    for ($page = 1; $page -le 5 -and -not $tag; $page++) {
        $api = "https://api.github.com/repos/$Repo/releases?per_page=100&page=$page"
        $releases = @(Invoke-RestMethod -Uri $api -UseBasicParsing)
        $tag = ($releases | Where-Object { $_.tag_name -like "ax-trace-v*" } | Select-Object -First 1).tag_name
        if ($releases.Count -lt 100) { break }
    }
    if (-not $tag) { throw "Could not resolve latest ax-trace version" }
    $Version = $tag -replace "^ax-trace-", ""
}

Write-Host "[ax-trace] Installing ax-trace $Version for windows_amd64"

$base = "https://github.com/$Repo/releases/download/ax-trace-$Version"
$archive = "ax-trace_$($Version -replace '^v','')_windows_amd64.zip"
$checksums = "checksums.txt"

$tmp = New-Item -ItemType Directory -Path "$env:TEMP\ax-trace-install-$(Get-Random)"
try {
    Invoke-WebRequest -Uri "$base/$archive" -OutFile "$tmp\$archive" -UseBasicParsing
    Invoke-WebRequest -Uri "$base/$checksums" -OutFile "$tmp\$checksums" -UseBasicParsing

    # Verify SHA256. GoReleaser format is "<hash>  <file>" — match the archive
    # filename at end of line so substring collisions can't match.
    $line = Get-Content "$tmp\$checksums" | Where-Object { $_ -match "  $([regex]::Escape($archive))$" } | Select-Object -First 1
    if (-not $line) { throw "Checksum entry for $archive not found in $checksums" }
    $expected = ($line -split "\s+")[0].ToLower()
    $actual = (Get-FileHash "$tmp\$archive" -Algorithm SHA256).Hash.ToLower()
    if ($expected -ne $actual) { throw "SHA256 verification failed for $archive" }

    Expand-Archive -Path "$tmp\$archive" -DestinationPath $tmp -Force
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    Copy-Item -Path "$tmp\ax-trace.exe" -Destination "$InstallDir\ax-trace.exe" -Force
    Write-Host "[ax-trace] Installed to $InstallDir\ax-trace.exe"

    # Add to user PATH if absent. Split on ';' and compare entries exactly so a
    # prefix-match (e.g. ax-trace vs ax-trace-old) can't false-positive.
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $entries = if ($userPath) { $userPath -split ";" | Where-Object { $_ } } else { @() }
    $normalizedInstall = $InstallDir.TrimEnd('\')
    $alreadyPresent = $entries | Where-Object { $_.TrimEnd('\') -eq $normalizedInstall }
    if (-not $alreadyPresent) {
        $newPath = if ($userPath) { "$userPath;$InstallDir" } else { $InstallDir }
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        Write-Host "[ax-trace] Added $InstallDir to user PATH (restart shell to take effect)"
    }
}
finally {
    Remove-Item -Recurse -Force $tmp
}

Write-Host ""
Write-Host "[ax-trace] Run 'ax-trace claude' to get started"
