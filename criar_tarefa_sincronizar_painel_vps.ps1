# criar_tarefa_sincronizar_painel_vps.ps1
#
# Cria a tarefa StokkiEventos_SincronizarPainelVPS -- mantem a replica de
# dados/dados.db + planilhas mestras que o painel_agentes na VPS
# (painel.freshhub.com.br, modo SOMENTE LEITURA) le. Roda a cada 15 min,
# o dia inteiro. A copia local continua sendo a UNICA fonte da verdade
# (Fase 1 do plano de migracao, DOC_EXECUCAO_CLAUDE_MIGRACAO_VPS.md) --
# este script so EMPURRA uma foto nova por cima da replica remota.
#
# RODE COMO ADMINISTRADOR, a partir de C:\agente_stokki_eventos:
#   .\criar_tarefa_sincronizar_painel_vps.ps1

$ErrorActionPreference = "Stop"

$TaskName = "StokkiEventos_SincronizarPainelVPS"
$Raiz     = "C:\agente_stokki_eventos"
# Caminho COMPLETO do py.exe -- existe um arquivo "py" fantasma (0 bytes)
# em C:\Windows\System32 que ganha da resolucao de comando e derruba a
# execucao (mesmo motivo do rodar_sequencial.ps1).
$Python   = "C:\Windows\py.exe"

$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"`"$Python`" -3.11 `"$Raiz\sincronizar_painel_vps.py`" >> `"$Raiz\dados\sincronizar_painel_vps_task.log`" 2>&1`"" `
    -WorkingDirectory $Raiz

$trigger = New-ScheduledTaskTrigger -Daily -At "00:00"
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At "00:00" `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration ([TimeSpan]::FromDays(1) - (New-TimeSpan -Minutes 1))).Repetition

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew

# Mesmo principal das outras tarefas (S4U, roda sem usuario logado)
$seqTarde = Get-ScheduledTask -TaskName "StokkiEventos_SequenciaTarde"

$existente = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existente) {
    Write-Host "Removendo registro anterior de '$TaskName'..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# -ErrorAction Stop explicito: sem ele, erro de "Acesso negado" (rodar sem
# admin) nao interrompe o script e a mensagem de sucesso sairia mesmo com
# a tarefa nao criada.
Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger `
    -Principal $seqTarde.Principal -Settings $settings `
    -Description "Sincroniza dados.db + planilhas mestras pra replica somente-leitura do painel_agentes na VPS, a cada 15 min. Fase 1 do plano de migracao pra VPS." `
    -Force -ErrorAction Stop | Out-Null

Write-Host ""
Write-Host "Tarefa '$TaskName' registrada." -ForegroundColor Green
Write-Host "Log das execucoes: $Raiz\dados\sincronizar_painel_vps_task.log"
Write-Host "Rodar manualmente uma vez (teste): Start-ScheduledTask -TaskName '$TaskName'"
