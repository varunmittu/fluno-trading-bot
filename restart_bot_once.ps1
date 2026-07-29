# One-shot safe restart (2026-07-14): kills the bot AFTER market close so the
# watchdog (start_bot.bat) restarts it with the new Confidence Score code.
# Finds the bot by its single-instance lock port 54501 — never touches the
# crypto/forex bots. Refuses to kill if a position is still open.
$log = Join-Path $PSScriptRoot "restart_once.log"
"[$(Get-Date)] restart script fired" | Out-File $log -Append -Encoding utf8
try {
    $state = (Invoke-WebRequest -Uri "http://127.0.0.1:5000/api/state" -UseBasicParsing -TimeoutSec 10).Content | ConvertFrom-Json
    if ($state.open_positions -gt 0 -or $state.pending_trade) {
        "[$(Get-Date)] ABORT — open position or pending trade" | Out-File $log -Append -Encoding utf8
        exit 1
    }
} catch {
    "[$(Get-Date)] state check failed ($($_.Exception.Message)) — proceeding (market closed)" | Out-File $log -Append -Encoding utf8
}
$c = Get-NetTCPConnection -LocalPort 54501 -ErrorAction SilentlyContinue | Select-Object -First 1
if ($c) {
    Stop-Process -Id $c.OwningProcess -Force
    "[$(Get-Date)] killed bot PID $($c.OwningProcess) — watchdog will restart it in ~10s" | Out-File $log -Append -Encoding utf8
} else {
    "[$(Get-Date)] no process on lock port 54501 — nothing to kill" | Out-File $log -Append -Encoding utf8
}
schtasks /delete /tn "varun_bot_restart_once" /f 2>$null
