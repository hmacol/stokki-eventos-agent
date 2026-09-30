# criar_tarefa_print_agent.ps1
#
# Cria a tarefa StokkiEventos_PrintAgent -- pergunta pra VPS a cada 10 min,
# das 04h05 as 07h00 (janela depois do stokki-romaneios-manha da VPS, 04h00,
# ate os motoristas saírem), se tem romaneio novo pra imprimir. So essa
# maquina fica responsavel por imprimir de verdade -- toda a decisao (quais
# rotas, o que entra em cada romaneio) ja roda na VPS.
#
# Fase 7 do plano de migracao pra VPS (DOC_EXECUCAO_CLAUDE_MIGRACAO_VPS.md).
#
# RODE COMO ADMINISTRADOR, a partir de C:\agente_stokki_eventos\print_agent:
#   .\criar_tarefa_print_agent.ps1

$ErrorActionPreference = "Stop"

$TaskName = "StokkiEventos_PrintAgent"
$Pasta    = "C:\agente_stokki_eventos\print_agent"
# Caminho COMPLETO do py.exe -- existe um arquivo "py" fantasma (0 bytes)
# em C:\Windows\System32 que ganha da resolucao de comando e derruba a
# execucao (mesmo motivo do rodar_sequencial.ps1).
$Python   = "C:\Windows\py.exe"

$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"`"$Python`" -3.11 `"$Pasta\print_agent.py`" >> `"$Pasta\dados\print_agent_task.log`" 2>&1`"" `
    -WorkingDirectory $Pasta

$trigger = New-ScheduledTaskTrigger -Daily -At "04:05"
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At "04:05" `
    -RepetitionInterval (New-TimeSpan -Minutes 10) `
    -RepetitionDuration (New-TimeSpan -Hours 2 -Minutes 55)).Repetition

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew

# S4U: roda mesmo sem o usuario estar logado -- precisa pra achar o
# SumatraPDF em AppData\Local do usuario e pra imprimir de verdade.
$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType S4U `
    -RunLevel Highest

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
    -Principal $Principal -Settings $settings `
    -Description "Print-agent (Fase 7 da migracao pra VPS): pergunta pra VPS a cada 10min (04h05-07h00) se tem romaneio novo e manda pra impressora padrao via SumatraPDF." `
    -Force -ErrorAction Stop | Out-Null

Write-Host ""
Write-Host "Tarefa '$TaskName' registrada." -ForegroundColor Green
Write-Host "Log das execucoes: $Pasta\dados\print_agent_task.log (e dados\print_agent.log do proprio script)"
Write-Host "Rodar manualmente uma vez (teste, SEM imprimir de verdade): py -3.11 print_agent.py --modo-teste"
