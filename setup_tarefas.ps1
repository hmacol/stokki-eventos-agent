# setup_tarefas.ps1
#
# Registra uma Tarefa Agendada do Windows que roda o pipeline completo
# (main.py) UMA VEZ POR DIA, às 18h30, em modo headless (sem janela
# visível) e já criando os pedidos de verdade na Stokki (modo real é o
# padrão agora).
#
# Rode este script UMA VEZ, como Administrador, no PowerShell:
#   cd C:\agente_importacao_stokki
#   .\setup_tarefas.ps1
#
# Pré-requisitos antes de rodar:
#   - config.yaml do cliente com TODAS as credenciais preenchidas
#     (email.senha_app e stokki.usuario/stokki.senha) — em modo automático
#     o script falha rápido se faltar algo, em vez de ficar esperando
#     digitação (o que travaria a tarefa pra sempre).
#   - Testado manualmente antes com:
#       python main.py "Empório Quatro Estrelas"
#     pra garantir que passa sem erro antes de deixar rodando sozinho.

$TaskName    = "AgenteImportacaoStokki_EmporioQuatroEstrelas"
$ProjectDir  = "C:\agente_importacao_stokki"
$PythonExe   = "python"   # troque para o caminho completo do python.exe se 'python' não estiver no PATH do sistema
$NomeCliente = "Empório Quatro Estrelas"

$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "main.py `"$NomeCliente`"" `
    -WorkingDirectory $ProjectDir

# Dispara todo dia às 18h30
$Trigger = New-ScheduledTaskTrigger -Daily -At "18:30"

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew  # se uma execução ainda estiver rodando, não inicia outra em cima

# S4U: roda mesmo sem o usuário estar logado, sem precisar guardar a senha do Windows
$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType S4U `
    -RunLevel Highest

# Remove uma tarefa antiga com o mesmo nome, se existir (registro anterior
# pode ter ficado pela metade se tiver dado erro)
$TarefaExistente = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($TarefaExistente) {
    Write-Host "Removendo registro anterior de '$TaskName' antes de recriar..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Roda o pipeline de importacao Stokki (Emporio Quatro Estrelas) 1x por dia as 18h30, headless, modo real (cria pedidos)." `
    -ErrorAction Stop

# Só chega aqui (e mostra sucesso) se Register-ScheduledTask não tiver
# lançado erro acima
Write-Host ""
Write-Host "Tarefa '$TaskName' registrada com sucesso." -ForegroundColor Green
Write-Host "Confira em: Agendador de Tarefas (Task Scheduler) -> Biblioteca do Agendador de Tarefas"
Write-Host ""
Write-Host "Para acompanhar as execucoes:"
Write-Host "  - Abra: $ProjectDir\clientes\emporio_quatro_estrelas\STATUS.md"
Write-Host "  - Ou no proprio Agendador de Tarefas, aba 'Historico' da tarefa"
Write-Host ""
Write-Host "Para rodar manualmente uma vez (teste): Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Para remover a tarefa depois: Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
