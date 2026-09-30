# diagnosticar_tarefas.ps1
#
# Mostra o status REAL de todas as tarefas agendadas do projeto --
# quando rodou pela última vez, se deu certo, e quando roda de novo.
# Pedido do Hugo, 04/08: "acho que meus agentes não estão rodando
# como programado".
#
# Diferente do resumo no final do setup_tarefas.ps1 (que só mostra
# TaskName/State), esse aqui mostra LastRunTime/LastTaskResult/
# NextRunTime -- o que realmente importa pra saber se rodou e se deu
# certo (LastTaskResult = 0 é sucesso; qualquer outro número é falha).
#
# COMO USAR:
#   PowerShell -ExecutionPolicy Bypass -File C:\agente_stokki_eventos\diagnosticar_tarefas.ps1

$tarefas = Get-ScheduledTask | Where-Object { $_.TaskName -like "StokkiEventos*" }

if ($tarefas.Count -eq 0) {
    Write-Host "Nenhuma tarefa 'StokkiEventos*' encontrada -- setup_tarefas.ps1 nunca foi rodado " -ForegroundColor Red
    Write-Host "(ou foi rodado sem privilégio de administrador e falhou silenciosamente)." -ForegroundColor Red
    exit
}

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host " Status das tarefas agendadas -- $(Get-Date -Format 'dd/MM/yyyy HH:mm')" -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host ""

$resultado = foreach ($tarefa in $tarefas) {
    $info = Get-ScheduledTaskInfo -TaskName $tarefa.TaskName

    $resultadoTexto = if ($null -eq $info.LastRunTime -or $info.LastRunTime -eq [datetime]"1999-11-30") {
        "NUNCA RODOU"
    } elseif ($info.LastTaskResult -eq 0) {
        "OK"
    } else {
        "FALHOU (código $($info.LastTaskResult))"
    }

    [PSCustomObject]@{
        Tarefa          = $tarefa.TaskName -replace "^StokkiEventos_", ""
        Estado          = $tarefa.State
        "Última vez"    = if ($info.LastRunTime -eq [datetime]"1999-11-30") { "-" } else { $info.LastRunTime }
        Resultado       = $resultadoTexto
        "Próxima vez"   = if ($tarefa.State -eq "Disabled") { "-" } else { $info.NextRunTime }
    }
}

$resultado | Sort-Object Tarefa | Format-Table -AutoSize

$falhando = $resultado | Where-Object { $_.Resultado -like "FALHOU*" }
$nuncaRodou = $resultado | Where-Object { $_.Resultado -eq "NUNCA RODOU" }

if ($falhando) {
    Write-Host "ATENÇÃO -- tarefas com falha na última execução:" -ForegroundColor Red
    $falhando | ForEach-Object { Write-Host "  - $($_.Tarefa): $($_.Resultado)" -ForegroundColor Red }
    Write-Host ""
}
if ($nuncaRodou) {
    Write-Host "Tarefas que nunca rodaram ainda (esperado se acabaram de ser criadas):" -ForegroundColor Yellow
    $nuncaRodou | ForEach-Object { Write-Host "  - $($_.Tarefa)" -ForegroundColor Yellow }
    Write-Host ""
}

Write-Host "Cole essa tabela inteira de volta no chat." -ForegroundColor Cyan
