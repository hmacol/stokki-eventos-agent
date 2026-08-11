# criar_tarefa_expedicao_frequente.ps1
#
# Cria a tarefa StokkiEventos_ExpedicaoFrequente -- a expedição saiu do
# executar_tudo.py em 06/08 pra rodar sozinha a cada 30 min (8h-19h35),
# mas a tarefa do Agendador nunca chegou a ser criada: entre 06/08 e
# 11/08 a expedição só rodou manualmente e ~570 pedidos entregues
# ficaram parados em "Aguardando Transportadora" (visto em 11/08).
# Este script fecha esse buraco.
#
# RODE COMO ADMINISTRADOR, a partir de C:\agente_stokki_eventos:
#   .\criar_tarefa_expedicao_frequente.ps1
#
# A tarefa roda o expedir_pedidos.py com a janela padrão (48h) -- em
# regime de 30 em 30 min isso cobre qualquer atraso de validação de
# canhoto de um dia pro outro. MultipleInstances IgnoreNew garante que
# uma execução longa (backlog) não acumula instâncias em cima.

$ErrorActionPreference = "Stop"

$TaskName = "StokkiEventos_ExpedicaoFrequente"
$Raiz     = "C:\agente_stokki_eventos"
# Caminho COMPLETO do py.exe -- existe um arquivo "py" fantasma (0 bytes)
# em C:\Windows\System32 que ganha da resolução de comando e derruba a
# execução (mesmo motivo do rodar_sequencial.ps1).
$Python   = "C:\Windows\py.exe"

$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"`"$Python`" -3.11 `"$Raiz\expedir_pedidos.py`" >> `"$Raiz\dados\expedicao_task.log`" 2>&1`"" `
    -WorkingDirectory $Raiz

# Todo dia às 08:00, repetindo a cada 30 min até 19:35 (última execução
# 19:30) -- janela definida em 06/08 pra detectar insucesso na entrega
# perto da ocorrência e dar tempo do embarcador responder antes das 20h.
$trigger = New-ScheduledTaskTrigger -Daily -At "08:00"
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At "08:00" `
    -RepetitionInterval (New-TimeSpan -Minutes 30) `
    -RepetitionDuration (New-TimeSpan -Hours 11 -Minutes 35)).Repetition

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew

# Mesmo principal das sequências (S4U, roda sem usuário logado)
$seqTarde = Get-ScheduledTask -TaskName "StokkiEventos_SequenciaTarde"

$existente = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existente) {
    Write-Host "Removendo registro anterior de '$TaskName'..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# -ErrorAction Stop explícito: sem ele, o erro CIM de "Acesso negado"
# (rodar sem admin) NÃO interrompe o script e a mensagem de sucesso
# abaixo sairia mesmo com a tarefa não criada (visto em 11/08).
Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger `
    -Principal $seqTarde.Principal -Settings $settings `
    -Description "Expedicao Stokki (expedir_pedidos.py) a cada 30 min, 08h-19h35: expede entregues com canhoto validado, anexa canhoto e trata insucessos." `
    -Force -ErrorAction Stop | Out-Null

Write-Host ""
Write-Host "Tarefa '$TaskName' registrada." -ForegroundColor Green
Write-Host "Log das execucoes: $Raiz\dados\expedicao_task.log (e dados\expedicao.log do proprio script)"
Write-Host "Rodar manualmente uma vez (teste): Start-ScheduledTask -TaskName '$TaskName'"
