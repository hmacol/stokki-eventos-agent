# Revisão de segurança — link de atualização do motorista (17/11/2026)

Escopo: `git diff c7dc23d origin/motoristas-padrao -- nucleo/api_motorista.py` (único módulo exposto
na internet tocado; +79/−7) e o que ele chama em `nucleo/atualizacao_motorista.py`. O ramo
`editar-rota-enviada` não toca módulo exposto.

Rotas novas (motorista-api, sem login): `GET /atualizar/<token>`,
`POST /api/atualizacao/<token>/{abrir,dados,concluir}`. Todas atrás de
`atualizacao_motoristas.ativo` (ausente = 503).

## Corrigidos antes do deploy (commit 8158c68)

- **ALTO — troca de PIX por quem tem o link e o CPF.** Operador/logística geravam o link e viam o CPF
  no painel; no grupo, qualquer participante que soubesse o CPF também. Agora: "Copiar link" só nível
  total, e trocar um PIX que já existia fica em `pix_pendente` até o nível total aprovar.
- **MÉDIO — Resetar PIN mostrava o PIN provisório a operador/logística.** Agora só nível total.
- Já corrigidos na execução: token no log (mascarado em todos os handlers), CPF reconferido no
  `concluir`, link de piloto não cancela link real, upload só com link vivo.

## Abertos (aceitos por enquanto; nada age com a chave desligada)

- **MÉDIO — linha sem CPF aceita o primeiro CPF válido e livre digitado.** Quem abrir o link primeiro
  fixa o CPF daquela linha. Mitigação atual: motorista sem CPF na tabela não entra no envio pelo grupo
  (só "Copiar link", nível total, mandado direto a ele).
- **MÉDIO — primeiro PIX entra direto** (quando não havia nenhum), sem aprovação.
- **BAIXO — `abrir` devolve PIX e nascimento pré-preenchidos** a quem tem o link e o CPF. Token de
  alta entropia, 5 tentativas de CPF por link, freio por IP.
- **BAIXO — login com lista de telas personalizada que inclua /motoristas vale como nível total**
  em `/api/motoristas` (pré-existente): conferir esses logins antes de ligar o envio real.

Sem achado ALTO aberto.
