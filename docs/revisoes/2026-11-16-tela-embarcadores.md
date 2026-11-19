# Revisão de segurança: tela /embarcadores (contas do portal)

Data: 16/11/2026. Ramo `embarcadores-tela`. Diff revisado: `origin/master..embarcadores-tela` em
`portal_cliente/auth_cliente.py`, `portal_cliente/gerenciar_clientes.py` e `painel_agentes/` (rotas
`/embarcadores` e `POST /api/embarcadores/conta`, template e testes). Revisão focada (triagem + leitura
dos fluxos de PIN e de link do portal); não é auditoria completa do portal.

## Achados

### MÉDIO, corrigido: link de PIN antigo voltava a valer depois de excluir a conta

`excluir_conta` apaga a linha de `clientes_portal`. Sem linha, `versao_conta` volta a ser `"novo"`, e
qualquer link de definir PIN emitido antes de a conta existir (convite, "convite pra repassar"), ainda
dentro das 24 h, passava de novo em `validar_token_definir_pin`. Quem tivesse o link antigo recriava a
conta e entrava.

Correção: a exclusão grava a marca `conta_excluida` em `portal_tentativas` (a tabela guarda 1 dia, o
mesmo prazo do link) e a validação recusa link emitido até o instante da exclusão. Teste:
`test_link_de_pin_antigo_nao_recria_conta_excluida`.

### MÉDIO, de comportamento, não corrigido (decisão do Hugo): excluir não bloqueia o cliente

O "Primeiro acesso / esqueci o PIN" do portal (já existente) manda link para qualquer embarcador de
`interno` com e-mail, tenha conta ou não. Então o cliente de conta excluída cria outra sozinho. Isso é
anterior a esta mudança. A tela e a confirmação do botão agora dizem isso e mandam usar Inativar para
bloquear (conta inativa não entra nem pelo link). Se excluir tiver de bloquear, é mudança de regra no
portal.

### BAIXO, corrigido: reset em conta inativa mandava e-mail de "acesso liberado"

O e-mail só sai para conta nova ou ativa.

## Conferido, sem achado

- Rotas novas só para nível total (`@requer_auth` sem `niveis`), POST com `exige_mesma_origem`; operador
  e origem de fora levam 403 (testes). Fora do catálogo de acesso personalizado.
- PIN sorteado com `secrets`, nunca gravado em log nem enviado no e-mail (o e-mail leva o link). Fica no
  `sessionStorage` do navegador só até a página recarregar.
- O CNPJ do corpo é normalizado e precisa existir em `interno` com `sender_id`; ação fora da lista leva 400.
- `enviar_convite` é o mesmo e-mail do `enviar-link`, extraído sem mudar texto nem destinatários.
- Limite já existente segue valendo: 5 tentativas de PIN, bloqueio de 15 minutos.

## Limites

Não revisei o restante do portal nem o envio de e-mail (`email_utils`). O nome do embarcador entra no HTML
do e-mail sem escape, como já era.
