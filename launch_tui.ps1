# launch_tui.ps1
# Drop in repo root. Run with: .\launch_tui.ps1
# Or right-click > Run with PowerShell

$repoPath = "C:\Users\caden\Desktop\prediction_market_bot"
$pythonCmd = "python"
$tuiScript = "tui.py"

# Check WezTerm is on PATH
if (-not (Get-Command "wezterm" -ErrorAction SilentlyContinue)) {
    Write-Error "wezterm not found on PATH. Make sure WezTerm is installed and added to PATH."
    exit 1
}

# Check tui.py exists
if (-not (Test-Path "$repoPath\$tuiScript")) {
    Write-Error "tui.py not found at $repoPath\$tuiScript"
    exit 1
}

# Launch WezTerm with the TUI (Geist Mono font)
wezterm `
    --config "font=wezterm.font('Geist Mono')" `
    --config "font_size=12.0" `
    start --cwd $repoPath -- $pythonCmd $tuiScript
