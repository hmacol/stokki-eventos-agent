# rodar_sequencial.ps1
#
# Roda uma lista de agentes em SEQUÊNCIA -- cada um só começa depois
# que o anterior TERMINOU DE VERDADE (não é um horário "chutado" com
# alguns minutos de espaço, é esperar o processo sair de verdade).
# Pedido do Hugo, 04/08: "dá pra programar pra um agente só começar
# depois que o outro finalizar?" -- resolve de vez a colisão no banco
# SQLite compartilhado que derrubou ExecutarTudo e Notificador quando
# os dois disparavam ao mesmo tempo (14h).
#
# Chamado pelo Agendador de Tarefas do Windows (task
# StokkiEventos_SequenciaTarde, 14:02) -- pode ser rodado manualmente
# também, pra testar.

$Raiz = "C:\agente_stokki_eventos"
$Python = "py"
$LogDir = "$Raiz\dados"

$passos = @(
    @{ Nome = "ExecutarTudo"; Script = "$Raiz\executar_tudo.py" }
    @{ Nome = "Notificador"; Script = "$Raiz\notificar_pedidos_em_espera.py" }
)

$logArquivo = "$LogDir\sequencia_tarde.log"
"" | Out-File -FilePath $logArquivo -Append -Encoding utf8
"====================================================================" | Out-File -FilePath $logArquivo -Append -Encoding utf8
"[$(Get-Date -Format 'dd/MM/yyyy HH:mm:ss')] Sequência iniciada" | Out-File -FilePath $logArquivo -Append -Encoding utf8

foreach ($passo in $passos) {
    $inicio = Get-Date
    "[$($inicio.ToString('HH:mm:ss'))] Iniciando: $($passo.Nome)" | Out-File -FilePath $logArquivo -Append -Encoding utf8
    Write-Host "Iniciando: $($passo.Nome)..."

    # -Wait garante que só volta quando o processo TERMINOU de verdade
    # (sucesso ou erro) -- é isso que faz o próximo passo só começar
    # depois que esse encerrou, sem depender de horário nenhum.
    $processo = Start-Process -FilePath $Python -ArgumentList @("-3.11", $passo.Script) `
        -WorkingDirectory $Raiz -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput "$LogDir\$($passo.Nome)_sequencia_stdout.log" `
        -RedirectStandardError "$LogDir\$($passo.Nome)_sequencia_stderr.log"

    $duracao = (Get-Date) - $inicio
    $codigo = $processo.ExitCode
    $resultado = if ($codigo -eq 0) { "OK" } else { "FALHOU (código $codigo)" }

    "[$(Get-Date -Format 'HH:mm:ss')] $($passo.Nome): $resultado -- levou $([math]::Round($duracao.TotalMinutes, 1)) min" `
        | Out-File -FilePath $logArquivo -Append -Encoding utf8
    Write-Host "  $($passo.Nome): $resultado"
}

"[$(Get-Date -Format 'HH:mm:ss')] Sequência concluída" | Out-File -FilePath $logArquivo -Append -Encoding utf8
Write-Host "Sequência concluída. Log completo em: $logArquivo"
