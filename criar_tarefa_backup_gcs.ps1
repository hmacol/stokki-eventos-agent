# criar_tarefa_backup_gcs.ps1
#
# Cria a tarefa StokkiEventos_BackupGCS -- roda backup_dados_gcs.py todo
# dia às 03:00 (janela quieta: depois da SequenciaNoite/22h, antes de
# RomaneiosManha/04h e NotificarTransportadoras/04h10). Envia um snapshot
# consistente de dados/dados.db (VACUUM INTO) + as 3 planilhas mestras pro
# GCS, com retenção de 30 dias.
#
# Parte da Fase 0 do plano de migração pra VPS
# (DOC_EXECUCAO_CLAUDE_MIGRACAO_VPS.md) -- rede de segurança ANTES de
# qualquer dado sair da máquina local, já que dados/* nunca foi versionado.
#
# RODE COMO ADMINISTRADOR, a partir de C:\agente_stokki_eventos:
#   .\criar_tarefa_backup_gcs.ps1

$ErrorActionPreference = "Stop"

$TaskName = "StokkiEventos_BackupGCS"
$Raiz     = "C:\agente_stokki_eventos"
# Caminho COMPLETO do py.exe -- existe um arquivo "py" fantasma (0 bytes)
# em C:\Windows\System32 que ganha da resolucao de comando e derruba a
# execucao (mesmo motivo do rodar_sequencial.ps1).
$Python   = "C:\Windows\py.exe"

$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"`"$Python`" -3.11 `"$Raiz\backup_dados_gcs.py`" >> `"$Raiz\dados\backup_gcs_task.log`" 2>&1`"" `
    -WorkingDirectory $Raiz

$trigger = New-ScheduledTaskTrigger -Daily -At "03:00"

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
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
    -Description "Backup diario (03h00) de dados/dados.db (snapshot consistente via VACUUM INTO) + planilhas mestras pro GCS, retencao 30 dias. Fase 0 do plano de migracao pra VPS." `
    -Force -ErrorAction Stop | Out-Null

Write-Host ""
Write-Host "Tarefa '$TaskName' registrada." -ForegroundColor Green
Write-Host "Log das execucoes: $Raiz\dados\backup_gcs_task.log"
Write-Host "Rodar manualmente uma vez (teste): Start-ScheduledTask -TaskName '$TaskName'"
