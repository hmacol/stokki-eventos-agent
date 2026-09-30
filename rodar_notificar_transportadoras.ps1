# rodar_notificar_transportadoras.ps1
#
# Notifica as transportadoras de redespacho (tipo TERCEIROS) com o XML
# da NF-e dos pedidos do dia, de manha cedo -- roda logo depois de
# StokkiEventos_RomaneiosManha (04:00), quando as rotas do dia ja
# passaram pelos incrementos da noite (StokkiEventos_SequenciaNoite).
# Ver DOC_EXECUCAO_CLAUDE_NOTIFICACAO_TRANSPORTADORAS.md.
#
# Chamado pelo Agendador de Tarefas do Windows (task
# StokkiEventos_NotificarTransportadoras, 04:10). Pode ser rodado
# manualmente tambem, pra testar.

$Raiz = "C:\agente_stokki_eventos"
# Caminho COMPLETO do launcher: existe um arquivo "py" (0 bytes, sem
# extensao) largado em C:\Windows\System32 que ganha do py.exe na
# resolucao do Start-Process e derruba a execucao com "%1 nao e um
# aplicativo Win32 valido" (mesmo workaround do rodar_romaneios_manha.ps1).
$Python = "C:\Windows\py.exe"
$LogDir = "$Raiz\dados"

$logArquivo = "$LogDir\sequencia_notificar_transportadoras.log"
"" | Out-File -FilePath $logArquivo -Append -Encoding utf8
"====================================================================" | Out-File -FilePath $logArquivo -Append -Encoding utf8
"[$(Get-Date -Format 'dd/MM/yyyy HH:mm:ss')] Notificacao de transportadoras iniciada" | Out-File -FilePath $logArquivo -Append -Encoding utf8

$inicio = Get-Date
$processo = Start-Process -FilePath $Python `
    -ArgumentList @("-3.11", "$Raiz\notificacao_transportadoras\notificar_transportadoras.py", "--data", "hoje") `
    -WorkingDirectory "$Raiz\notificacao_transportadoras" -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput "$LogDir\NotificarTransportadoras_sequencia_stdout.log" `
    -RedirectStandardError "$LogDir\NotificarTransportadoras_sequencia_stderr.log"

$duracao = (Get-Date) - $inicio
$codigo = $processo.ExitCode
$resultado = if ($codigo -eq 0) { "OK" } else { "FALHOU (código $codigo)" }

"[$(Get-Date -Format 'HH:mm:ss')] NotificarTransportadoras: $resultado -- levou $([math]::Round($duracao.TotalMinutes, 1)) min" `
    | Out-File -FilePath $logArquivo -Append -Encoding utf8
Write-Host "NotificarTransportadoras: $resultado. Log completo em: $logArquivo"
