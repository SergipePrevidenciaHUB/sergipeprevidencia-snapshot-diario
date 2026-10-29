# SergipePrevidência - Rotina Diária Automática de Snapshot (Windows -> GitHub Pages)
$ErrorActionPreference = "Stop"
Set-Location "C:\Users\BSPBarreto\.gemini\antigravity\scratch\sergipeprevidencia-wayback"

$logFile = "C:\Users\BSPBarreto\.gemini\antigravity\scratch\sergipeprevidencia-wayback\snapshot_auto.log"
Add-Content -Path $logFile -Value "=== Iniciando Snapshot Diário em $(Get-Date -Format 'dd/MM/yyyy HH:mm:ss') ==="

try {
    git pull --rebase origin main 2>&1 | Out-String | Add-Content -Path $logFile
    python snapshot_archiver.py --output archive --workers 12 2>&1 | Out-String | Add-Content -Path $logFile
    git add archive/ 2>&1 | Out-String | Add-Content -Path $logFile
    $snapDate = Get-Date -Format "yyyy-MM-dd HH:mm"
    git commit -m "chore(snapshot): backup diario automatico [$snapDate]" 2>&1 | Out-String | Add-Content -Path $logFile
    git push origin main 2>&1 | Out-String | Add-Content -Path $logFile
    Add-Content -Path $logFile -Value "=== SUCESSO em $(Get-Date -Format 'dd/MM/yyyy HH:mm:ss') ===`n"
} catch {
    Add-Content -Path $logFile -Value "[ERRO] $($_.Exception.Message)`n"
}
