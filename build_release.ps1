$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$releaseDir = Join-Path $projectRoot "release"
$stageDir = Join-Path $releaseDir "JobHunterJP"
$zipPath = Join-Path $releaseDir "JobHunterJP_v1_sanitized.zip"

$include = @(
    "agent.py",
    "browser.py",
    "scorer.py",
    "qc.py",
    "form_ai.py",
    "logger.py",
    "setup_wizard.py",
    "run.bat",
    "dry_run.bat",
    "requirements.txt",
    "config.example.yaml",
    ".gitignore",
    "build_release.ps1",
    "README.md",
    "SECURITY.md"
)

if (Test-Path -LiteralPath $stageDir) {
    Remove-Item -LiteralPath $stageDir -Recurse -Force
}
New-Item -ItemType Directory -Path $stageDir | Out-Null

foreach ($name in $include) {
    $source = Join-Path $projectRoot $name
    if (-not (Test-Path -LiteralPath $source)) {
        throw "Missing release file: $name"
    }
    Copy-Item -LiteralPath $source -Destination (Join-Path $stageDir $name) -Force
}

if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
Compress-Archive -Path $stageDir -DestinationPath $zipPath -Force

$forbiddenNames = @(
    ".env",
    "config.yaml",
    "applications_log.xlsx",
    "ai_form_answers.log",
    "unanswered_questions.log",
    "form_events.log",
    "seen_jobs.json",
    "__pycache__"
)

Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::OpenRead($zipPath)
try {
    foreach ($entry in $zip.Entries) {
        $entryName = Split-Path -Leaf $entry.FullName
        foreach ($bad in $forbiddenNames) {
            if ($entryName -eq $bad -or $entry.FullName -like "*/$bad/*") {
                throw "Forbidden file found in release zip: $($entry.FullName)"
            }
        }
    }
}
finally {
    $zip.Dispose()
}

Write-Host "Created sanitized release zip: $zipPath"
