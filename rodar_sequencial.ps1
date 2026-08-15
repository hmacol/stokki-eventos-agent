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
# StokkiEventos_SequenciaTarde, 18:00). Pedido do Hugo, 10/08: depois
# do ExecutarTudo entra a verificação de duplicados no VUUPT (limpa
# antes de criar rota em cima de duplicidade), CriarRotasDiarias cria
# as rotas do próximo dia útil, o Notificador avisa embarcadores com
# os dados já corrigidos por essa mesma execução, e por último
# ProcessarDocumentos roda depois das notificações (não trava nem
# atrasa o aviso ao cliente). Os incrementos de hora em hora à noite
# (task StokkiEventos_SequenciaNoite, 22:00) já encontram as rotas
# criadas aqui -- pode ser rodado manualmente também, pra testar.
#
# CriarRotasDiarias roda com --gerar-rascunho (pedido do Hugo, 13/08):
# as rotas do dia não vão mais direto pra VUUPT, ficam paradas em
# /planejamento pra revisão manual até alguém clicar "Confirmar e
# Enviar". Sem confirmação manual até lá, IncrementarRotas
# (rodar_sequencial_noite.ps1, 22h) não acha nenhuma rota de hoje na
# VUUPT e FALHA de propósito, em vez de criar rota nova por cima do
# que já está no rascunho -- o envio automático de rascunho pendente
# que cobria esse buraco foi cancelado (pedido do Hugo, 14/08),
# confirmar em /planejamento agora é obrigatório.

$Raiz = "C:\agente_stokki_eventos"
# Caminho COMPLETO do launcher: existe um arquivo "py" (0 bytes, sem
# extensão) largado em C:\Windows\System32 que ganha do py.exe na
# resolução do Start-Process e derrubava a sequência inteira com
# "%1 não é um aplicativo Win32 válido" (visto em 10/08, 18h).
$Python = "C:\Windows\py.exe"
$LogDir = "$Raiz\dados"

$passos = @(
    @{ Nome = "ExecutarTudo"; Script = "$Raiz\executar_tudo.py"; Cwd = $Raiz }
    @{ Nome = "VerificarDuplicadosVuupt"; Script = "$Raiz\verificar_pedidos_duplicados_vuupt.py"; Cwd = $Raiz }
    @{ Nome = "CriarRotasDiarias"; Script = "$Raiz\roteirizacao\criar_rotas_diarias.py"; Args = @("--gerar-rascunho"); Cwd = "$Raiz\roteirizacao" }
    @{ Nome = "Notificador"; Script = "$Raiz\notificar_pedidos_em_espera.py"; Cwd = $Raiz }
    @{ Nome = "ProcessarDocumentos"; Script = "$Raiz\documentos_pedido\processar_documentos.py"; Cwd = "$Raiz\documentos_pedido" }
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
    $argumentos = @("-3.11", $passo.Script) + $(if ($passo.Args) { $passo.Args } else { @() })
    $processo = Start-Process -FilePath $Python -ArgumentList $argumentos `
        -WorkingDirectory $passo.Cwd -NoNewWindow -Wait -PassThru `
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
