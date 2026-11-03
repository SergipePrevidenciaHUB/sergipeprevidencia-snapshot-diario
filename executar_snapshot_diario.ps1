# SergipePrevidência - Rotina Diária Automática de Snapshot (Windows -> GitHub Pages)
$ErrorActionPreference = "Continue"
if (Get-Variable -Name "PSNativeCommandUseErrorActionPreference" -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}
Set-Location "C:\Users\BSPBarreto\.gemini\antigravity\scratch\sergipeprevidencia-wayback"

$logFile = "C:\Users\BSPBarreto\.gemini\antigravity\scratch\sergipeprevidencia-wayback\snapshot_auto.log"

function Write-Log($msg) {
    $time = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $logFile -Value "[$time] $msg"
}

Write-Log "=== INICIANDO SNAPSHOT DIÁRIO ==="

# 1. Puxar possíveis atualizações remotas
Write-Log "Executando git pull..."
git pull --rebase origin main *>> $logFile
Write-Log "git pull concluído (código $LASTEXITCODE)."

# 2. Executar captura das páginas do portal
Write-Log "Executando snapshot_archiver.py..."
python snapshot_archiver.py --output archive --workers 12 *>> $logFile
$pythonExit = $LASTEXITCODE
if ($pythonExit -ne 0) {
    Write-Log "[ERRO CRÍTICO] snapshot_archiver encerrou com código de erro $pythonExit."
    exit $pythonExit
}
Write-Log "snapshot_archiver.py concluído com sucesso."

# 3. Adicionar arquivos e verificar se houve alterações
git add archive/ *>> $logFile
$hasChanges = git status --porcelain archive/

if ($hasChanges) {
    $snapDate = Get-Date -Format "yyyy-MM-dd HH:mm"
    Write-Log "Criando commit com o novo snapshot [$snapDate]..."
    git commit -m "chore(snapshot): backup diario automatico [$snapDate]" *>> $logFile
    Write-Log "Enviando para o GitHub (git push)..."
    git push origin main *>> $logFile
    if ($LASTEXITCODE -eq 0) {
        Write-Log "=== SUCESSO: Snapshot diário concluído e enviado com sucesso ao GitHub! ==="
    } else {
        Write-Log "[ERRO] git push falhou com código $LASTEXITCODE."
    }
} else {
    Write-Log "Nenhuma alteração detectada para commit hoje."
}

Write-Log "=== FIM DA EXECUÇÃO ===`n"
