# Revisão de segurança: login de grupo envia sem seletor de empresa (coluna CNPJ na planilha)

- Data: 16/11/2026
- Diff revisado: `git diff 46485f2 origin/master -- portal_cliente` (produção em 46485f2, master em cb8b764, PR #26)
- Arquivos de produção no diff: `app.py` (176 linhas), `envio_pedidos.py` (120), `anexos_pedido.py` (7), `templates/_envios.html` (41). Testes: `test_envio_planilha.py`, `test_grupos.py`.
- Risco da mudança: ALTO por natureza (decide para qual cliente da Stokki o pedido vai, em módulo exposto na internet).
- Resultado: **nenhum achado ALTO ou MÉDIO**. Dois pontos BAIXOS, abaixo.

## O que mudou no controle de acesso

Antes, o login de grupo escolhia a empresa por requisição (`X-Portal-Empresa` / `?empresa=`), validada contra `g.cliente["empresas"]`. Agora, sem o cabeçalho, a empresa sai de cada item: coluna da planilha, emitente do XML, ou dono do envio na rota com id.

## Pontos verificados

1. **Empresa de fora do grupo na planilha.** `ler_planilha(..., empresas=)` só aceita CNPJ que esteja no dicionário montado por `_grupo_sem_seletor()`, que parte de `g.cliente["empresas"]` (sessão do servidor). Fora disso o pedido é rejeitado. Teste: rejeições em `test_envio_planilha.py`.
2. **Confirmação não confia no navegador.** `confirmar_envios` relê o temporário gravado pelo servidor e tira a empresa do emitente dele; `_contexto()` levanta erro se o emitente não estiver em `por_empresa`. Toda a validação roda antes da primeira gravação. Teste: "empresa de fora não grava nada".
3. **Rotas com id de envio.** `_empresa_do_envio` só devolve empresa que esteja em `g.cliente["empresas"]`; envio de outro cliente cai na empresa do login e `buscar_envio(conn, id, cnpj)` dá 404 como antes. Sem diferença de resposta entre "não existe" e "é de outro".
4. **Modo misto (com e sem cabeçalho).** Analisar sem cabeçalho e confirmar com ele (ou o contrário) cai na checagem antiga de emitente igual à empresa, ou na checagem nova de emitente dentro do grupo. Não há combinação que pule as duas.
5. **Tokens de PDF.** O dono passou da empresa para o CNPJ do login nos quatro pontos (`guardar_previa` x2, `validar_tokens`, `anexar(dono=)`). Fica igual para login avulso e mais restrito no grupo (login do grupo e conta própria de um membro deixam de enxergar os tokens um do outro).
6. **Mensagens de erro.** As novas ecoam só o CNPJ já reduzido a dígitos e formatado; a tela usa `esc()` em `empresaDe` e nos erros.
7. **Tokens temporários.** `secrets.token_urlsafe(18)` (144 bits), sem mudança.
8. **Equipe com nível sem envio.** `_exige_pode_enviar()` continua na frente de confirmar e reenviar.

## Achados

### BAIXO 1: temporário de planilha sem dono
O JSON temporário do pedido de planilha não guarda quem o criou (já era assim). No grupo, a checagem deixou de ser "emitente igual à empresa do envio" e passou a "emitente dentro do grupo". Para abusar, outro login autorizado na mesma empresa teria de adivinhar um token de 144 bits. Sem exploração prática; melhoria possível: gravar o CNPJ do login no temporário, como os PDFs já fazem.

### BAIXO 2: prévia aberta na hora do deploy
PDF guardado por login de grupo antes do restart tem como dono a empresa do seletor; depois do restart o dono esperado é o login. Uma prévia em andamento nesse instante recebe "documento inválido" e o cliente precisa subir o PDF de novo. Não é falha de segurança.

## Limites desta revisão

- Feita por quem escreveu a mudança; não é leitura independente.
- O worker que cria o pedido na Stokki (`enviar_stokki.py`) não está no diff e não foi relido; o caminho por planilha nele nunca rodou em produção.
- Suíte no código mesclado: 660 passam. As duas falhas vistas no worktree temporário vêm da falta do banco local e passam no repositório principal.
- A versão mesclada (com a ação "Alterar endereço de entrega") não passou pela homologação; o OK de 14/11 foi sobre 288ee7c.
