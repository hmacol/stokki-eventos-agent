# criar_tarefa_validacao_checklists.ps1
#
# Cria a tarefa StokkiEventos_ValidacaoChecklists -- o agente de
# validacao de checklists (validacao_checklists\validar_checklists.py)
# roda a cada 30 min, 10 MINUTOS ANTES de cada execucao da expedicao
# frequente (que dispara em :00/:30): valida os canhotos aprovados via
# API do VUUPT e o expedir_pedidos.py ja os expede no mesmo ciclo.
#
# RODE COMO ADMINISTRADOR, a partir de C:\agente_stokki_eventos:
#   .\criar_tarefa_validacao_checklists.ps1

$ErrorActionPreference = "Stop"

$TaskName = "StokkiEventos_ValidacaoChecklists"
$Raiz     = "C:\agente_stokki_eventos"
# Caminho COMPLETO do py.exe -- existe um arquivo "py" fantasma (0 bytes)
# em C:\Windows\System32 que ganha da resolucao de comando e derruba a
# execucao (mesmo motivo do rodar_sequencial.ps1).
$Python   = "C:\Windows\py.exe"

$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"`"$Python`" -3.11 `"$Raiz\validacao_checklists\validar_checklists.py`" >> `"$Raiz\dados\validacao_checklists_task.log`" 2>&1`"" `
    -WorkingDirectory "$Raiz\validacao_checklists"

# Todo dia as 07:50, repetindo a cada 30 min ate 19:25 (ultima execucao
# 19:20) -- sempre 10 min antes da expedicao frequente (08:00-19:30).
$trigger = New-ScheduledTaskTrigger -Daily -At "07:50"
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At "07:50" `
    -RepetitionInterval (New-TimeSpan -Minutes 30) `
    -RepetitionDuration (New-TimeSpan -Hours 11 -Minutes 35)).Repetition

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

# Mesmo principal das sequencias (S4U, roda sem usuario logado)
$seqTarde = Get-ScheduledTask -TaskName "StokkiEventos_SequenciaTarde"

$existente = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existente) {
    Write-Host "Removendo registro anterior de '$TaskName'..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# -ErrorAction Stop explicito: sem ele, o erro CIM de "Acesso negado"
# (rodar sem admin) NAO interrompe o script e a mensagem de sucesso
# abaixo sairia mesmo com a tarefa nao criada (visto em 11/08).
Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger `
    -Principal $seqTarde.Principal -Settings $settings `
    -Description "Validacao de checklists (validar_checklists.py) a cada 30 min, 07h50-19h25: aprova canhotos via IA, valida no VUUPT e clona pedidos com foto fora do padrao." `
    -Force -ErrorAction Stop | Out-Null

Write-Host ""
Write-Host "Tarefa '$TaskName' registrada." -ForegroundColor Green
Write-Host "Log das execucoes: $Raiz\dados\validacao_checklists_task.log (e dados\validacao_checklists.log do proprio script)"
Write-Host "Rodar manualmente uma vez (teste): Start-ScheduledTask -TaskName '$TaskName'"
