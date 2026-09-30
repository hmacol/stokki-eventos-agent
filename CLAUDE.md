# agente_stokki_eventos

Automação logística da Fresh Log (Fresh Hub): integra o WMS **Stokki** (pedidos, NF, expedição, via Playwright + HTTP), o roteirizador **Vuupt** (rotas, agentes, checklists, via API), o **app dos motoristas** (Expo, em `app_motorista/`), o **portal do cliente** e o **painel interno** (Flask). Rotinas em lote rodam por timers systemd na VPS; telas web ficam atrás do Caddy em `app.freshhub.com.br/<serviço>`.

Idioma do projeto: português (código, comentários, commits, respostas). Comentários e strings de log sem acento quando o arquivo já segue esse padrão.

**Antes de qualquer alteração, consulte `MAPA_DO_SISTEMA.txt`** (raiz): diz onde fica cada coisa, que script roda quando, portas, tabelas e armadilhas. Use-o para achar os arquivos certos em vez de sair procurando. Se criar, mover ou apagar arquivo/rotina/tela, atualize o mapa no mesmo commit.

## Ambiente local (Windows 10, máquina do Hugo)

- Python: **sempre `py -3.11`** (3.11.7). `python`/`py` sem versão caem no 3.13 ou em stubs da Microsoft Store. Em `.ps1`/tarefas agendadas use o caminho completo `C:\Windows\py.exe`.
- Saída com acento quebra em processo sem console: `PYTHONIOENCODING=utf-8` está no settings; scripts novos que rodam agendados reconfiguram `sys.stdout` pra UTF-8 no topo.
- PowerShell é o 5.1: sem `&&`, sem `??`, sem `?.`. Prefira a ferramenta Bash (Git Bash) pra encadear comandos.
- `config.yaml` é o arquivo de segredos (gitignored). A VPS tem o dela, com seções diferentes. Nunca commitar, nunca sobrescrever, nunca colar valores dele no chat.
- Banco: `dados/dados.db` (SQLite, 45+ tabelas). O de produção é o da VPS; o local está congelado desde 17/08 e serve só pra desenvolvimento.
- Painel local (`painel_agentes/`, porta 8070) pode estar rodando de verdade. Teste **sempre em outra porta**:
  `py -3.11 -c "import painel_agentes; painel_agentes.app.run(host='127.0.0.1', port=8099)"` (de dentro de `painel_agentes/`). Nunca `Stop-Process` em PID que já existia antes da sessão.
- Só o `print_agent/` continua no Agendador do Windows (04h05 até 07h, a cada 10 min). O resto foi pra VPS.

## Produção (VPS Hostinger, Ubuntu 24.04)

- Acesso: `ssh -i ~/.ssh/atendimento_vps root@187.127.52.197`. Repo em `/opt/stokki-eventos`, dono `www-data:www-data`. Timezone `America/Sao_Paulo`.
- Interpretador: **`/opt/stokki-eventos/venv/bin/python`**, nunca `python3` do sistema.
- Rodar scripts como o usuário dos serviços: `sudo -u www-data venv/bin/python script.py`. Rodar como root cria arquivos (logs, STATUS_*.md, WAL do SQLite) que depois travam o serviço.
- Serviços (systemd): `painel-agentes` (waitress 127.0.0.1:8071), `motorista-api` (8073), `portal-cliente` (8074), `atendimento-central` (8095), `confirmacao-motoristas` (8090, em `/opt/confirmacao-motoristas`, cópia manual). Timers em `infra/*.timer`; o `.service` par define o comando.
- `Type=oneshot` fica em `activating` enquanto roda. Pra esperar terminar use `systemctl show -p ActiveState --value <unit>` e aguarde sair de `activating`; `is-active --quiet` mente.
- Antes de criar timer/unit novo: `systemctl list-timers` e `ls /etc/systemd/system/`. O Hugo mexe na VPS direto e pode já ter feito.
- Segundo projeto: `/opt/agente-importacao-stokki` (repo `c:\agente_importacao_stokki`), timers `importacao-stokki-*`. Compartilha a mesma sessão Stokki: login concorrente derruba a outra sessão com 401.

## Deploy (só depois de commit + push)

1. `ssh ... "cd /opt/stokki-eventos && git status --short && git log --oneline -3"` (ver se alguém deixou coisa lá).
2. `git pull --no-rebase` (o histórico da VPS tem merges; `--ff-only` costuma falhar).
3. `chown -R www-data:www-data /opt/stokki-eventos` (pull como root cria arquivo como root).
4. Se mudou unit/timer: copiar de `infra/` pra `/etc/systemd/system/`, `systemctl daemon-reload`, `systemctl enable --now <timer>`.
5. `systemctl restart <serviço afetado>` só dos serviços que importam o módulo alterado. Módulos em lote (timers) pegam a versão nova sozinhos.
6. Provar que funciona de verdade: chamar a rota/função alterada (curl na porta local, ou `venv/bin/python -c`), não só `systemctl is-active`.

## Verificação antes de entregar

- `py -3.11 -m py_compile <arquivos>` em tudo que editou (o hook faz isso automático em Edit/Write).
- Testes existentes: `py -3.11 -m unittest <modulo>` (ex.: `roteirizacao/test_*.py`, `portal_cliente/test_*.py`, `regras/test_*.py`). Não há pytest configurado.
- Scripts de lote aceitam `--modo-teste` (não escreve em Stokki/Vuupt, e-mails vão pra `forcar_destino`). Use antes de qualquer execução real.
- Playwright/Stokki: use as funções prontas em `stokki/` (`StokkiSession`, `contar_pedidos`, `listar_transportadoras`...). Chamada manual ao DataTables da Stokki dá 500.
- Vuupt: `created_at`/`start_at` vêm em UTC sem fuso. Códigos de pedido variam (`#PS-1`, `PS-1`, `#PS-1-R2`): normalizar com `normalizar_order_number` antes de comparar.

## Trabalho em paralelo (importante)

O Hugo e outras sessões do Claude editam e commitam neste mesmo working tree e na VPS ao vivo, durante a sessão.
- Antes de editar e de novo antes de commitar: `git status --short` e `git log --oneline -5`.
- `git diff` vazio num arquivo que você editou significa que um commit alheio absorveu a edição. Confira `git log -1` antes de reescrever.
- Só `git add` dos arquivos da sua tarefa. Nunca `git add -A`.
- Commit e push só quando o Hugo pedir ("commit", "deploy"). Sem `--force`, sem `reset --hard`, sem rebase de histórico já publicado.
- Modificações não commitadas que você não fez (ex.: `infra/*.service`) são de outra sessão. Não reverta, não inclua no seu commit.

## Convenções

- Commits: `Área: o que mudou e por quê`, em português (ex.: `Painel: menu lateral agrupado por momento do dia`).
- Telas novas seguem `app.freshhub.com.br/<serviço>`: Caddy `handle_path` + `ProxyFix(x_prefix=1)` + `url_for()` (nunca caminho absoluto no template).
- Envio real pra clientes/motoristas fica desligado por padrão (`notificacoes_automaticas.ativo`, `forcar_destino=hugo@`). Ligar é decisão do Hugo.
- Especificações por feature ficam em `DOC_EXECUCAO_CLAUDE_*.md` na raiz. Leia o do módulo antes de mexer em algo grande.
- Decisões de negócio ambíguas (termos, cortes de horário, quem recebe e-mail): perguntar a fonte ao Hugo antes de implementar.
