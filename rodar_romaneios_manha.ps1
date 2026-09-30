# rodar_romaneios_manha.ps1
#
# Gera os PDFs de romaneio (1 por rota do dia, com NFs e boletos na
# ordem de visita) de manhã cedo, ANTES da saída dos motoristas --
# nesse horário as rotas já passaram pelos incrementos da noite
# (StokkiEventos_SequenciaNoite) e os documentos processados às 18h
# já estão no índice. Ver DOC_EXECUCAO_CLAUDE_ROMANEIOS_PDF.md.
#
# Chamado pelo Agendador de Tarefas do Windows (task
# StokkiEventos_RomaneiosManha, 04:00). Pode ser rodado manualmente
# também, pra testar.

$Raiz = "C:\agente_stokki_eventos"
# Caminho COMPLETO do launcher: existe um arquivo "py" (0 bytes, sem
# extensão) largado em C:\Windows\System32 que ganha do py.exe na
# resolução do Start-Process e derruba a execução com "%1 não é um
# aplicativo Win32 válido" (mesmo workaround do rodar_sequencial.ps1).
$Python = "C:\Windows\py.exe"
$LogDir = "$Raiz\dados"

$logArquivo = "$LogDir\sequencia_romaneios.log"
"" | Out-File -FilePath $logArquivo -Append -Encoding utf8
"====================================================================" | Out-File -FilePath $logArquivo -Append -Encoding utf8
"[$(Get-Date -Format 'dd/MM/yyyy HH:mm:ss')] Geração de romaneios iniciada" | Out-File -FilePath $logArquivo -Append -Encoding utf8

$inicio = Get-Date
$processo = Start-Process -FilePath $Python `
    -ArgumentList @("-3.11", "$Raiz\roteirizacao\gerar_pdf_romaneios.py", "--data", "hoje") `
    -WorkingDirectory "$Raiz\roteirizacao" -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput "$LogDir\RomaneiosManha_sequencia_stdout.log" `
    -RedirectStandardError "$LogDir\RomaneiosManha_sequencia_stderr.log"

$duracao = (Get-Date) - $inicio
$codigo = $processo.ExitCode
$resultado = if ($codigo -eq 0) { "OK" } else { "FALHOU (código $codigo)" }

"[$(Get-Date -Format 'HH:mm:ss')] GerarRomaneios: $resultado -- levou $([math]::Round($duracao.TotalMinutes, 1)) min" `
    | Out-File -FilePath $logArquivo -Append -Encoding utf8
Write-Host "GerarRomaneios: $resultado. Log completo em: $logArquivo"
