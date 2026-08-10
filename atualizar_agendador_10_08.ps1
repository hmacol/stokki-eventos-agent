# atualizar_agendador_10_08.ps1
#
# Aplica as 6 mudanças de agendamento pedidas pelo Hugo em 10/08:
#   1. ExecutarTudo passa a rodar às 13h no lugar de CriarRotasDiarias
#      (CriarRotasDiarias continua rodando, só que dentro da
#      SequenciaTarde, 18h)
#   2. Remove a tarefa solta IncrementarRotas (19h-00h de hora em
#      hora) -- passa a rodar só dentro da SequenciaNoite (22h)
#   3. Notificador passa a rodar às 7h E às 16h (fora de qualquer
#      sequência)
#   4. Cria a tarefa SequenciaNoite (22h): ExecutarTudo +
#      VerificarDuplicadosVuupt + IncrementarRotas
#   (as mudanças de ProcessarDocumentos e VerificarDuplicadosVuupt
#   dentro da SequenciaTarde já estão em rodar_sequencial.ps1 -- não
#   precisam de tarefa nova, só do conteúdo do script, que já foi
#   editado)
#
# RODE COMO ADMINISTRADOR (botão direito no PowerShell -> Executar
# como administrador), a partir de C:\agente_stokki_eventos:
#   .\atualizar_agendador_10_08.ps1
#
# Todas as tarefas ficam no MESMO estado (Enabled/Disabled) em que já
# estavam -- este script não liga nem desliga nada, só redesenha os
# horários/conteúdo.

$ErrorActionPreference = "Stop"

Write-Host "1) ExecutarTudo -> triggers 10:00 e 13:00 (remove 19:10, ja coberto pela SequenciaTarde)" -ForegroundColor Cyan
$triggersET = @(New-ScheduledTaskTrigger -Daily -At "10:00"; New-ScheduledTaskTrigger -Daily -At "13:00")
Set-ScheduledTask -TaskName "StokkiEventos_ExecutarTudo" -Trigger $triggersET | Out-Null

Write-Host "2) Removendo tarefa solta StokkiEventos_CriarRotasDiarias (13h) -- CriarRotasDiarias continua na SequenciaTarde" -ForegroundColor Cyan
Unregister-ScheduledTask -TaskName "StokkiEventos_CriarRotasDiarias" -Confirm:$false -ErrorAction SilentlyContinue

Write-Host "3) Removendo tarefa solta StokkiEventos_IncrementarRotas -- passa a rodar so na SequenciaNoite (22h)" -ForegroundColor Cyan
Unregister-ScheduledTask -TaskName "StokkiEventos_IncrementarRotas" -Confirm:$false -ErrorAction SilentlyContinue

Write-Host "4) Notificador -> triggers 07:00 e 16:00" -ForegroundColor Cyan
$triggersNot = @(New-ScheduledTaskTrigger -Daily -At "07:00"; New-ScheduledTaskTrigger -Daily -At "16:00")
Set-ScheduledTask -TaskName "StokkiEventos_Notificador" -Trigger $triggersNot | Out-Null

Write-Host "5) Criando StokkiEventos_SequenciaNoite (22:00)" -ForegroundColor Cyan
$seqTarde = Get-ScheduledTask -TaskName "StokkiEventos_SequenciaTarde"
$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument '/c "PowerShell -ExecutionPolicy Bypass -File "C:\agente_stokki_eventos\rodar_sequencial_noite.ps1" >> "C:\agente_stokki_eventos\dados\sequencia_noite_task.log" 2>&1"' `
    -WorkingDirectory "C:\agente_stokki_eventos"
$trigger = New-ScheduledTaskTrigger -Daily -At "22:00"

Register-ScheduledTask -TaskName "StokkiEventos_SequenciaNoite" `
    -Action $action -Trigger $trigger `
    -Principal $seqTarde.Principal -Settings $seqTarde.Settings `
    -Description "ExecutarTudo + VerificarDuplicadosVuupt + IncrementarRotas em sequencia, 22h." `
    -Force | Out-Null

Write-Host ""
Write-Host "Concluido. Rode diagnosticar_tarefas.ps1 pra conferir os novos horarios." -ForegroundColor Green
