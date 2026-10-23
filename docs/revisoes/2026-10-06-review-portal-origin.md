# Revisão de segurança: portal_cliente/ em origin/master (produção)

Data: 06/10/2026 (fechada em 07/10). Revisor: Claude (somente leitura).
Alvo: `git diff master origin/master -- portal_cliente` (master local 68be71e é ancestral direto; origin/master é o que roda em app.freshhub.com.br/cliente, porta 8074).
Commits revisados: 05d1769, 6d4a205, 481bccb, 9a9a754, 46c61cc, 43b823f, 1a27bf2, ef73e44, 6f00580 (mais merges cc395f7, 3327765, ed92629).
Todas as linhas citadas são da versão **origin/master** (`git show origin/master:portal_cliente/<arquivo>`).

## Resumo executivo

1. O lote de contas com PIN padrão 123456 (9a9a754) deixa 21 contas abríveis por qualquer pessoa que saiba o CNPJ do cliente (dado público) até o próprio cliente trocar o PIN; não há aviso ao cliente nem prazo de validade, e o atacante que entra primeiro passa a ser o dono da conta. O fluxo de troca em si está bem fechado (não pula, invalida sessões antigas).
2. Reenviar em lote (46c61cc) e cancelamento solicitado (6f00580) respeitam a autorização por CNPJ/grupo; o risco deles é operacional (fila da Stokki, cobrança por e-mail continua), não de acesso indevido.
3. Dos 6 achados de 15/09, 2 foram corrigidos (3d10157), 4 continuam abertos sem mudança: HTML sem escape no e-mail à operação, xlsx sem neutralizar fórmulas, /equipe/cliente e /sair sem anti-CSRF, e-mails livres na proposta de cotação (hoje contido só pelo `forcar_destino`).

## Arquivos alterados x risco

| Arquivo | Risco | Por quê | Commits |
|---|---|---|---|
| portal_cliente/auth_cliente.py | ALTO | PIN padrão em lote, troca obrigatória, `sessao_valida` | 9a9a754 |
| portal_cliente/app.py | ALTO | before_request bloqueando até a troca, rota `/trocar-pin`, `/api/envios/reenviar-erros`, aviso fora do dia fixo no confirmar, WhatsApp nas preferências | 9a9a754, 46c61cc, 43b823f, 6d4a205 |
| portal_cliente/gerenciar_clientes.py | ALTO | CLI `criar-pin-padrao` (cria contas em lote), `--cc` no convite | 9a9a754, 05d1769 |
| portal_cliente/enviar_stokki.py | MÉDIO | credenciais da Stokki vindas da trava por conta; espera de 2h; filtro `status='ENVIADO'` | 1a27bf2, ef73e44 |
| portal_cliente/envio_pedidos.py | MÉDIO | `reenviar_erros`, `avisos_fora_dia_fixo` (relê temporário por token), `_linha` com cancelamento, dedicado por código | 46c61cc, 43b823f, 6f00580 |
| portal_cliente/templates/trocar_pin.html | BAIXO | formulário novo (Jinja autoescape) | 9a9a754 |
| portal_cliente/templates/_envios.html | BAIXO | botão "Reenviar todos", modal fora do dia fixo (usa `esc()`) | 46c61cc, 43b823f |
| portal_cliente/templates/_notificacoes.html | BAIXO | campo WhatsApp | 481bccb |
| portal_cliente/test_*.py (8 arquivos) | BAIXO | testes | vários |

## Achados

### A1. ALTO -- PIN padrão compartilhado: quem souber o CNPJ entra na conta antes do cliente

- Onde: `auth_cliente.py:286-306` (`candidatos_pin_padrao`, `criar_contas_pin_padrao`), `gerenciar_clientes.py:288-296` (CLI), `app.py:276-299` (`/login` sem trava por IP).
- Introduzido em: 9a9a754 (03/10). Executado em produção em 03/10: 21 contas com PIN 123456 (memória do projeto).
- Cenário: um ex-funcionário de um embarcador, um concorrente ou qualquer pessoa que tenha uma NF da Fresh Log em mãos conhece o CNPJ dos clientes (CNPJ é público; o remetente aparece em toda DANFE). Acessa `/cliente/login`, digita CNPJ + 123456. Se o cliente ainda não entrou, o login funciona na 1ª tentativa (a trava de 5 erros em `autenticar`, `auth_cliente.py:339-348`, é por conta e nunca é tocada numa tentativa certa; não existe trava por IP em `/login`). O atacante cai em `/trocar-pin`, define um PIN próprio e passa a ser o dono: vê todas as entregas, canhotos (`/api/canhoto`), telefones de destinatários no `/exportar.xlsx`, envia e cancela pedidos, altera e-mails/WhatsApp de notificação. Como o grupo econômico também ganhou conta (`candidatos_pin_padrao` só exclui o *membro*, não o *login* do grupo), um único acerto abre várias empresas. O cliente legítimo só percebe quando o 123456 falhar; ao pedir "esqueci o PIN", o `definir_pin` troca o hash e derruba o atacante (`versao_conta`), mas nada do que foi lido/feito é desfeito nem avisado.
- Agravante: segundo a memória, o WhatsApp avisando os clientes ainda não foi enviado, então a janela está aberta por tempo indeterminado e ninguém está olhando a conta.
- Correção sugerida: (a) expirar o PIN padrão: em `sessao_valida`/`autenticar`, recusar conta com `trocar_pin=1` cujo `criado_em` tenha mais de N dias (ex.: 7) e exigir o link de e-mail; (b) ao concluir `/trocar-pin`, mandar e-mail aos `emails` de `interno` ("seu PIN foi definido em DD/MM às HH:MM; não foi você? fale com a Fresh Log") -- é o único sinal que o cliente teria; (c) trava de tentativas por IP em `/login` (mesmo `portal_tentativas`, escopo `login_ip`), que hoje só existe em `/equipe` e `/primeiro-acesso`.

### A2. MÉDIO -- Não há registro nem alerta do primeiro uso do PIN padrão

- Onde: `app.py:417-430` (`trocar_pin` só faz `logger.info`), `auth_cliente.py:350-351` (`ultimo_login_em` é sobrescrito a cada login).
- Introduzido em: 9a9a754.
- Cenário: continuação de A1. Depois do takeover não há como saber de que IP veio a troca, nem quando a conta foi aberta pela primeira vez; `ultimo_login_em` já terá sido sobrescrito pelos logins seguintes e o `logger.info` vive só no journal do serviço.
- Correção: gravar `primeiro_login_em` e `pin_trocado_em`/IP em `clientes_portal` na troca; listar no `gerenciar_clientes.py checar`.

### A3. MÉDIO -- `/trocar-pin` e `/definir-pin` sem `exige_mesma_origem`

- Onde: `app.py:406-407` e `app.py:371-372` (sem decorador; compare com `app.py:779-780`).
- Introduzido em: 9a9a754 (`/trocar-pin`); `/definir-pin` é de aaf931a1 (08/09, pré-existente).
- Cenário: o cookie é `SameSite=Lax` (`app.py:82`), o que bloqueia o POST cross-site com cookie nos navegadores atuais; o risco real fica restrito a navegadores antigos ou a um subdomínio de freshhub.com.br comprometido (site-for-cookies é o mesmo). Nesse caso um formulário auto-submetido troca o PIN da vítima com `trocar_pin=1` sem ela saber.
- Correção: aplicar `@exige_mesma_origem` nas duas rotas (uma linha cada); custa nada e alinha com o resto dos POSTs.

### A4. MÉDIO -- "Reenviar todos" permite martelar a fila da Stokki sem limite

- Onde: `app.py:828-851`, `envio_pedidos.py:1538-1549`, `enviar_stokki.py:571-577` (espera agora até `ESPERA_MAXIMA_SEGUNDOS` = 2h, com `alternativa=True`).
- Introduzido em: 46c61cc (botão) + 1a27bf2 (fila de 2h).
- Cenário: cliente (ou equipe nível operador) com 50 pedidos em ERRO por causa permanente (SKU inexistente, CEP inválido) clica "Reenviar todos" a cada minuto. Cada clique devolve tudo pra NA_FILA, o worker pega a trava da Stokki (ou a conta provider), falha de novo, volta pra ERRO. Com a fila de 2h de 1a27bf2, o worker do portal fica ocupando a sessão Stokki compartilhada com o importador (`/opt/agente-importacao-stokki`) e as rotinas de expedição, que passam a esperar. Não é um ataque a outro cliente, mas é negação de serviço acidental ou deliberada na integração inteira.
- Correção: em `reenviar_erros`, pular envios cujo `atualizado_em` seja de menos de X minutos ou que já tenham N solicitações `reenviar` CONCLUIDA (`portal_solicitacoes`); devolver a contagem de "pulados" na mensagem.

### A5. BAIXO -- Cancelamento solicitado para de cobrar só na tela; os e-mails/vigia continuam

- Onde: `envio_pedidos.py:1338-1346` (`_linha` zera `agendamento_pendente` apenas no dict devolvido ao portal; o banco não muda).
- Introduzido em: 6f00580 (06/10).
- Cenário: o caso que motivou o commit (PS-39959, 2 semanas de "Definir data") só é resolvido no portal. `roteirizacao/notificar_agendamento_pendente.py`, `vigia/vigiar.py:161-231` e `criar_rotas_diarias.py:103` leem `agendamento_pendente` do banco/VUUPT e não olham `portal_solicitacoes` tipo `cancelar`; o cliente continua recebendo cobrança por e-mail de um pedido que pediu pra cancelar. Não é falha de autorização (a solicitação é lida com `cnpj_embarcador = ?`, `envio_pedidos.py:1381`), é inconsistência.
- Correção: decidir com o Hugo se a solicitação de cancelamento deve também marcar algo no banco (ex.: `portal_envios.agendamento_pendente = 0` ou um status `CANCELAMENTO_SOLICITADO`) que as rotinas de cobrança respeitem.

### A6. BAIXO -- `avisos_fora_dia_fixo` roda antes de validar a empresa e lê o temporário de qualquer token

- Onde: `app.py:794` chamado antes de `config_stokki_cliente(... _empresa_envio() ...)` em `app.py:797`; `envio_pedidos.py:1164-1192`.
- Introduzido em: 43b823f.
- Cenário: o temporário é identificado só por token (`secrets.token_urlsafe(18)`, `envio_pedidos.py:840`), sem vínculo com o CNPJ da sessão. Um cliente logado que obtenha o token de outro (não há caminho conhecido pra isso; o token nunca é listado) consegue ler nome do destinatário, região e data da NF alheia via a resposta 409. O `_caminho_temporario` (`envio_pedidos.py:851-858`) valida o formato (`[A-Za-z0-9_-]{10,40}`), então não há path traversal. Risco baixo, registrado porque `confirmar_envios` tem o mesmo desenho e a nova função amplia a superfície (devolve dados da NF antes de gravar).
- Correção: gravar o CNPJ no nome ou no conteúdo do temporário e conferir em `_caminho_temporario`.

### A7. BAIXO -- PIN padrão passa pela linha de comando

- Onde: `gerenciar_clientes.py:240-242, 288-296` (`criar-pin-padrao 123456 --confirmar`).
- Introduzido em: 9a9a754.
- Cenário: o PIN fica no histórico do shell da VPS (`/root/.bash_history`) e em `ps` enquanto roda. Dado que é 123456 e é compartilhado, o vazamento adicional é marginal; relevante só se um dia o lote usar PIN não trivial.
- Correção: ler o PIN de variável de ambiente ou prompt (`getpass`), ou gerar um por conta e mandar por link em vez de PIN fixo.

### A8. INFO -- Número de WhatsApp de terceiros nas preferências

- Onde: `app.py:618-625`, `preferencias_notificacao.py:164-174, 221-241`.
- Introduzido em: 6d4a205 / 481bccb.
- Cenário: cliente (ou atacante de A1) grava qualquer celular brasileiro válido; a Fresh Log passa a mandar "chamado aguardando resposta" pra esse número pelo OpenWA. Texto é fixo (sem injeção de conteúdo), há teto de 50/dia e hoje `forcar_destino` segura o envio real, então é só assédio de baixo volume em nome da Fresh Log. Sem correção necessária agora; vale lembrar quando o envio real for ligado.

### A9. INFO -- Credenciais da Stokki do worker do portal agora podem cair na conta "provider"

- Onde: `enviar_stokki.py:122-133` (`_credenciais` usa `stokki.contas.credenciais(config, conta_do_processo() or "principal")` quando `portal_cliente.stokki` não tem usuário/senha).
- Introduzido em: 1a27bf2.
- Observação: não é vulnerabilidade; é mudança de qual conta cria pedidos na Stokki em nome do cliente. A memória diz que a conta wms@ não acessa `/administrator`; se a criação de pedido precisar de algo que a conta alternativa não tem, o lote do portal falha e cai em ERRO (alimentando A4). Verificar no log do worker qual conta ele tem usado.

## Perguntas do novo foco (PIN padrão, 9a9a754)

- **O fluxo de troca pode ser pulado?** Não encontrei caminho. `_carregar_cliente` (`app.py:134-138`) roda em todo request de sessão de cliente e só libera os endpoints `trocar_pin`, `sair`, `static`, `saude`; as rotas dos blueprints (`chamados_web`, `cotacao_web`, `link_canhoto`) também passam por ele. `/api/*` recebe 403 JSON, o resto redirect. A flag vem do banco a cada request (`sessao_valida`, `auth_cliente.py:370-373`), não do cookie, então não dá pra forjar. A equipe (`session["equipe"]`) nunca passa por essa checagem porque o ramo dela retorna antes (`app.py:118-124`), o que é correto: ela não usa PIN.
- **O PIN padrão serve pra entrar em outra conta?** Sim, em qualquer uma das 21 que ainda não trocou -- é o A1. Não há vazamento *entre* contas já trocadas: `trocar_pin_obrigatorio` opera só em `g.cliente["cnpj"]` (sessão), nada vem do formulário além do PIN.
- **Há rate limit?** Só o pré-existente por conta (5 erros, 15 min, `auth_cliente.py:37-38, 339-348`). Nenhum por IP em `/login`. Irrelevante contra A1, que acerta na 1ª tentativa.
- **O token de sessão muda após a troca?** Sim. `definir_pin` gera salt e hash novos (`auth_cliente.py:269-270`), `versao_conta` (prefixo do hash) muda, `trocar_pin` faz `session.clear()` e grava a versão nova (`app.py:425-428`); qualquer outra sessão com a versão velha cai em `sessao_valida`. Também invalida o link de e-mail pendente (payload `v`).
- **Mesmo PIN de novo?** Recusado por comparação de hash (`auth_cliente.py:315-316`). Não há lista de PINs fracos (111111, 123456 vindo de outra conta etc.) -- aceitável pra PIN de 6 dígitos.
- **Cancelamento solicitado (6f00580) e reenviar erros (46c61cc): autorização por CNPJ/grupo está certa?** Sim. `reenviar_erros` filtra `cnpj_embarcador = ?` com o CNPJ vindo de `_empresa_envio()` (`app.py:639-651`), que só aceita CNPJ presente em `g.cliente["empresas"]` (403 caso contrário; coberto por `test_grupos.py::test_reenviar_erros_na_empresa_do_grupo_escolhida`). `_exige_pode_enviar()` barra equipe nível leitura. `_linha` recebe solicitações já filtradas por `cnpj_embarcador` (`envio_pedidos.py:1381`). Nada do corpo do request escolhe o CNPJ.

## Achados antigos (15/09): estado

| Achado | Estado | Evidência |
|---|---|---|
| /equipe sem trava de tentativas | **CORRIGIDO** em 3d10157 (17/09) | `app.py:224-252`: 5 erros por IP em 15 min via `portal_tentativas` (escopo `equipe_ip`); testes `test_acesso_travas.py:148-154`. Observação: trava por IP é contornável trocando de IP, e o `remote_addr` depende do `X-Forwarded-For` que o Caddy injeta (ProxyFix `x_for=1`); fora do Caddy seria forjável. |
| "Primeiro acesso" enumera CNPJ e dispara e-mail sem limite | **CORRIGIDO** em 3d10157 | `app.py:308-351`: resposta única (`mensagem.html` "Confira seu e-mail"), 3 links/CNPJ/h e 20 pedidos/IP/h; testes `test_acesso_travas.py:115-135`. Resta um oráculo de tempo pré-existente em `autenticar` (`auth_cliente.py:331-332` retorna antes do PBKDF2 pra CNPJ sem conta) -- irrelevante enquanto A1 existir. |
| e-mail pra operação sem `html.escape` em motivo/data/horas | **ABERTO**, sem mudança | agora em `app.py:889-894` (`_avisar_operacao_solicitacao`): `corpo['motivo'][:300]`, `corpo.get('data')`, `hora_inicio`, `hora_fim` entram no HTML crus. Não há `import html` no arquivo. Blame aaf931a1 (08/09). |
| xlsx exportado sem neutralizar `=`, `+`, `-`, `@` | **ABERTO**, sem mudança | `app.py:985-986`: `ws.append([p.get(chave, "") ...])` direto; nomes de destinatário e observações vêm da VUUPT/portal. |
| POST /equipe/cliente sem `exige_mesma_origem`; /sair é GET | **ABERTO**, sem mudança | `app.py:255-256` (sem decorador), `app.py:302-305` (GET). Mitigado parcialmente por `SameSite=Lax`; `/sair` via link ainda desloga. Agora `trocar_pin.html` linka `url_for('sair')` e `/trocar-pin` nasceu com o mesmo defeito (A3). |
| cotacao_web.py: proposta vai pra qualquer e-mail do JSON | **ABERTO**, contido por config | `cotacao_web.py:199-206`: `emails` vem do corpo sem conferir contra `_emails_cliente`; `cotacao.py:657-662` só redireciona se `forcar_destino` estiver preenchido (hoje está, pra hugo@, e a calculadora está oculta). No dia em que `forcar_destino` for esvaziado, um cliente logado manda PDF de proposta da Fresh Log pra qualquer endereço. |

## Cobertura de testes

Novos/alterados: `test_pin_padrao.py` (165 linhas), `test_reenviar_erros.py`, `test_cancelamento_agendamento.py`, `test_fora_dia_fixo_envio.py`, `test_grupos.py` (+1), `test_notificacoes_portal.py` (+3), ajustes de schema em `test_bloqueio_area.py` e `test_reconciliar_codigos.py`. Não executei os testes (o working tree tem edições de terceiros; rodar contra ele não provaria origin/master).

Cobre bem:
- Candidatos do lote (conta existente, cancelado, membro de grupo, sem sender) e idempotência.
- Troca obrigatória: redirect de `/`, 403 em `/api/dia`, recusa do mesmo PIN e de PINs diferentes, sessão continua válida após a troca, equipe/sem login não veem a tela.
- `reenviar_erros` só da empresa e só ERRO; grupo: empresa fora do acesso dá 403.
- WhatsApp: normalização, ausente mantém, inválido 400.
- Fora do dia fixo: data no dia/fora, aceite, pendente, inválida, passada, token expirado.

Sem teste (caminhos de erro/abuso):
- Login com PIN padrão em conta de *outro* CNPJ (o cenário A1) -- não há teste que documente que isso é esperado/aceito, nem de expiração (porque não existe).
- Sessão antiga derrubada após `/trocar-pin` (dois test_clients; o teste atual só confere que a sessão nova funciona).
- Equipe nível `leitura` em `/api/envios/reenviar-erros` (403 por `_exige_pode_enviar`) e `exige_mesma_origem` com Origin estranho nessa rota.
- `/trocar-pin` com Origin cross-site (ligado a A3).
- `avisos_fora_dia_fixo` com token em formato inválido (`../x`) -- `_caminho_temporario` cobre, mas o teste novo só usa token inexistente.
- `reenviar_erros` repetido N vezes (A4).
- `candidatos_pin_padrao` com `interno.observacoes` em caixa mista/espaços ("  cancelado") -- o `LIKE 'CANCELADO%'` com `UPPER` cobre caixa mas não espaço inicial.

## Limites desta revisão

- Li apenas o diff e, inteiros, `app.py` e `auth_cliente.py` em origin/master. De `envio_pedidos.py` li os trechos 835-862, 1143-1192, 1335-1406, 1452-1549; de `enviar_stokki.py` só o diff; de `cotacao_web.py`/`cotacao.py` só as linhas da proposta. Não li `chamados_web.py`, `link_canhoto.py`, `dados_cliente.py`, `bloqueio_area.py`, `preferencias_notificacao.py` além do grep, nem os templates fora do diff (`base_acesso.html`, `equipe.html`, `login.html`).
- Não li `stokki/contas.py` nem `stokki/sessao_uso.py` além de confirmar que os símbolos existem; A9 é observação, não análise.
- Não rodei nenhum teste nem o app; não acessei a VPS (estado real das 21 contas, quantas já trocaram o PIN, journal do serviço).
- Não revisei o working tree local (tem edições não commitadas de outras sessões).
- Não auditei o Caddyfile: a confiança em `remote_addr` (travas por IP) pressupõe que só o Caddy fala com a porta 8074.
