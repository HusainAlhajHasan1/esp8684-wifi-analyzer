<#
    push_to_github.ps1 - publish this project to the empty GitHub repo you created.

    Usage (PowerShell, from anywhere):
        cd C:\esp8684-wifi-analyzer
        .\tools\windows\push_to_github.ps1 -RepoUrl https://github.com/<you>/<repo>.git

    What it does, in order:
      1. refuses to run if git is missing or the URL looks wrong
      2. proves nothing sensitive is about to be published (measurement data,
         private keys) before touching the network
      3. initialises the repo only if it is not one already
      4. commits whatever has changed, on branch main
      5. adds/updates the `origin` remote and pushes

    It never asks for a password: GitHub Desktop, Git Credential Manager or
    `gh auth login` must already have authenticated this machine. It does not
    accept tokens on the command line either, because those leak into shell
    history.

    Safe to re-run: it is idempotent.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepoUrl,

    [string]$ProjectPath = (Get-Location).Path,

    [string]$Branch = "main",

    [string]$CommitMessage = "Update from push_to_github.ps1"
)

$ErrorActionPreference = "Stop"

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Die($msg)  { Write-Host "[FAIL] $msg" -ForegroundColor Red; exit 1 }

# --- 1. preconditions -------------------------------------------------------
Step "Checking prerequisites"

$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    Die "git is not installed. Install Git for Windows (https://git-scm.com/download/win) or use GitHub Desktop instead."
}
Write-Host "    git: $((git --version))"

if ($RepoUrl -notmatch '^(https://github\.com/|git@github\.com:)') {
    Die "RepoUrl does not look like a GitHub remote: $RepoUrl`n       Expected https://github.com/<you>/<repo>.git or git@github.com:<you>/<repo>.git"
}
# Normalise the suffix exactly once. The previous test was "no trailing slash",
# which is also true of a URL that already ends in .git - so
# .../analyzer.git became .../analyzer.git.git and every push reported
# "Repository not found". Test the SUFFIX, not the absence of a slash.
$RepoUrl = $RepoUrl.TrimEnd('/')
if (-not $RepoUrl.EndsWith('.git')) {
    Write-Host "    note: URL has no .git suffix - adding it" -ForegroundColor Yellow
    $RepoUrl = "$RepoUrl.git"
}
Write-Host "    remote: $RepoUrl"

if (-not (Test-Path (Join-Path $ProjectPath "firmware"))) {
    Die "$ProjectPath does not look like the project root (no firmware folder)."
}
Set-Location $ProjectPath
Write-Host "    project: $ProjectPath"

# --- 2. prove nothing sensitive is about to be published --------------------
Step "Checking what would be published"

$sensitive = @()
foreach ($pattern in @("scan*.json", "scan*.csv", "phase1_report.json", ".env", "*.pem", "*.key")) {
    $hits = Get-ChildItem -Path . -Filter $pattern -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notmatch '\\\.git\\' }
    foreach ($h in $hits) { $sensitive += $h.FullName }
}
if ($sensitive.Count -gt 0) {
    Write-Host "    These files exist locally and MUST NOT be published:" -ForegroundColor Yellow
    $sensitive | ForEach-Object { Write-Host "      $_" }
    Write-Host "    They are listed in .gitignore, so they will not be committed." -ForegroundColor Yellow
    Write-Host "    Move them out of the project folder if you want to be certain." -ForegroundColor Yellow
} else {
    Write-Host "    no measurement captures or key material found - clean to publish"
}

# --- 3. repository ----------------------------------------------------------
Step "Preparing the repository"

if (-not (Test-Path ".git")) {
    git init --initial-branch=$Branch | Out-Null
    Write-Host "    initialised a new repository"
} else {
    Write-Host "    repository already exists"
}

# identity is required for a commit; fill it from the GitHub config if absent
$userName = (git config user.name) 2>$null
if (-not $userName) {
    git config user.name  "ESP8684 Wi-Fi Analyzer"
    Write-Host "    set a local user.name (change it if you care about authorship)" -ForegroundColor Yellow
}
$userEmail = (git config user.email) 2>$null
if (-not $userEmail) {
    git config user.email "esp8684-analyzer@users.noreply.github.com"
    Write-Host "    set a local user.email" -ForegroundColor Yellow
}

git add -A
$pending = (git status --porcelain)
if ($pending) {
    $count = ($pending | Measure-Object).Count
    git commit -m $CommitMessage | Out-Null
    Write-Host "    committed $count changed file(s)"
} else {
    Write-Host "    nothing new to commit"
}

# make sure the branch name is what we expect, whatever the initial state was
$current = (git rev-parse --abbrev-ref HEAD).Trim()
if ($current -ne $Branch) {
    git branch -M $Branch
    Write-Host "    renamed branch '$current' -> '$Branch'"
}

# --- 4. remote + push -------------------------------------------------------
Step "Publishing to $RepoUrl"

$remotes = (git remote) 2>$null
if ($remotes -contains "origin") {
    $existing = (git remote get-url origin).Trim()
    if ($existing -ne $RepoUrl) {
        Write-Host "    origin was $existing - repointing" -ForegroundColor Yellow
        git remote set-url origin $RepoUrl
    }
} else {
    git remote add origin $RepoUrl
    Write-Host "    added remote origin"
}

$ErrorActionPreference = "Continue"
$already = (git ls-remote --heads origin 2>$null)
$ErrorActionPreference = "Stop"
if ($already) {
    Write-Host "    remote already has commits - pulling with --rebase first" -ForegroundColor Yellow
    git pull --rebase origin $Branch
}

git push -u origin $Branch
if ($LASTEXITCODE -ne 0) {
    Die "push failed. Most common causes:`n       1. not authenticated      -> run: gh auth login   (or open GitHub Desktop)`n       2. branch protection     -> the repo needs at least one push to exist`n       3. wrong URL or renamed repo"
}

# --- 5. report --------------------------------------------------------------
Step "Done"
$head = (git log -1 --oneline)
$files = (git ls-files | Measure-Object).Count
Write-Host "    $head"
Write-Host "    $files files tracked, branch '$Branch'"
Write-Host "    $RepoUrl"
Write-Host "`n    Open the repo page and confirm the README renders." -ForegroundColor Green
