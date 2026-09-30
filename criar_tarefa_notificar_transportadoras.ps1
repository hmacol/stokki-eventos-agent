# criar_tarefa_notificar_transportadoras.ps1
#
# Cria a tarefa StokkiEventos_NotificarTransportadoras -- notifica as
# transportadoras de redespacho (tipo TERCEIROS) com o XML da NF-e dos
# pedidos do dia. Roda 1x por dia, as 04:10 -- 10 min depois de
# StokkiEventos_RomaneiosManha (04:00), so por precaucao de concorrencia
# (os dois fluxos nao compartilham estado; a unica dependencia real e
# que as rotas do dia ja estejam montadas, o que StokkiEventos_
# SequenciaNoite (22h) ja garante bem antes disso).
#
# RODE COMO ADMINISTRADOR, a partir de C:\agente_stokki_eventos:
#   .\criar_tarefa_notificar_transportadoras.ps1

$ErrorActionPreference = "Stop"

$TaskName = "StokkiEventos_NotificarTransportadoras"
$Raiz     = "C:\agente_stokki_eventos"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Raiz\rodar_notificar_transportadoras.ps1`""

$trigger = New-ScheduledTaskTrigger -Daily -At "04:10"

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
# abaixo sairia mesmo com a tarefa nao criada (mesmo cuidado de
# criar_tarefa_validacao_checklists.ps1).
Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger `
    -Principal $seqTarde.Principal -Settings $settings `
    -Description "Notifica transportadoras de redespacho (TERCEIROS) com o XML da NF-e dos pedidos do dia, com copia ao(s) embarcador(es)." `
    -Force -ErrorAction Stop | Out-Null

Write-Host ""
Write-Host "Tarefa '$TaskName' registrada." -ForegroundColor Green
Write-Host "Log das execucoes: $Raiz\dados\sequencia_notificar_transportadoras.log (e dados\notificar_transportadoras.log do proprio script)"
Write-Host "Rodar manualmente uma vez (teste): Start-ScheduledTask -TaskName '$TaskName'"
