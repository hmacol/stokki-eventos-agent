# rodar_sequencial_noite.ps1
#
# Igual ao rodar_sequencial.ps1 (SequenciaTarde), mas pra passada da
# noite: cada agente só começa depois que o anterior TERMINOU DE
# VERDADE. Pedido do Hugo, 10/08.
#
# Chamado pelo Agendador de Tarefas do Windows (task
# StokkiEventos_SequenciaNoite, 22:00): ExecutarTudo faz a última
# correção de pedidos do dia (agendamento/insucesso/impressão/
# importação), VerificarDuplicadosVuupt limpa cópias sobressalentes
# antes do incremento, e por último IncrementarRotas aloca na rota do
# dia qualquer pedido novo que tenha entrado depois da
# CriarRotasDiarias da tarde -- pode ser rodado manualmente também,
# pra testar.
#
# O envio automático de rascunho pendente (EnviarRascunhosPendentes)
# foi CANCELADO daqui em 14/08 (mandava QUALQUER pendente, cego demais)
# e REVIVIDO em 23/08 como Fase 3 do roadmap de roteirização (portão
# automático): volta a rodar antes do IncrementarRotas, mas agora só
# manda pra VUUPT o rascunho pendente aprovado na nota de qualidade
# (zero badges da tela /planejamento + motorista atribuído) -- o resto
# continua em RASCUNHO e o IncrementarRotas continua FALHANDO de
# propósito (e-mail de alerta) pra esses, forçando a confirmação
# manual em vez de mascarar o esquecimento (mesmo espírito do Hugo em
# 14/08, só que agora a maioria não precisa mais de confirmação manual
# nenhuma).
#
# Produção roda isso na VPS (infra/sequencia_noite.sh, systemd timer),
# não mais por este .ps1 -- ver memória do projeto (migração VPS). Este
# arquivo fica só pra rodar/testar localmente no Windows.

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
    @{ Nome = "EnviarRascunhosPendentes"; Script = "$Raiz\roteirizacao\enviar_rascunhos_pendentes.py"; Cwd = "$Raiz\roteirizacao" }
    @{ Nome = "IncrementarRotas"; Script = "$Raiz\roteirizacao\incrementar_rotas.py"; Cwd = "$Raiz\roteirizacao" }
)

$logArquivo = "$LogDir\sequencia_noite.log"
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
        -WorkingDirectory $passo.Cwd -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput "$LogDir\$($passo.Nome)_sequencia_noite_stdout.log" `
        -RedirectStandardError "$LogDir\$($passo.Nome)_sequencia_noite_stderr.log"

    $duracao = (Get-Date) - $inicio
    $codigo = $processo.ExitCode
    $resultado = if ($codigo -eq 0) { "OK" } else { "FALHOU (código $codigo)" }

    "[$(Get-Date -Format 'HH:mm:ss')] $($passo.Nome): $resultado -- levou $([math]::Round($duracao.TotalMinutes, 1)) min" `
        | Out-File -FilePath $logArquivo -Append -Encoding utf8
    Write-Host "  $($passo.Nome): $resultado"
}

"[$(Get-Date -Format 'HH:mm:ss')] Sequência concluída" | Out-File -FilePath $logArquivo -Append -Encoding utf8
Write-Host "Sequência concluída. Log completo em: $logArquivo"
