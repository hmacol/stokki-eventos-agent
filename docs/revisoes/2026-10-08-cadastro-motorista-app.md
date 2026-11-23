# Revisão de segurança — cadastro do motorista pelo app (08/10/2026, noite)

Escopo (working tree, ainda sem commit): `nucleo/cadastro_motorista.py` (novo),
`nucleo/api_motorista.py` (PUT /api/eu, POST /api/cadastro, POST /api/cadastro/<id>/documentos),
`nucleo/banco.py` (tabelas), `regras/cadastro_motoristas.py` (atualizar_contato_planilha),
`painel_agentes/painel_agentes.py` (aprovar/recusar/documento), `painel_agentes/templates/motoristas.html`,
`notificar_whatsapp.py` (2 textos), app (`meus-dados.tsx`, `cadastro.tsx`, `api.ts`).
Feita pela mesma sessão que escreveu o código; revisão automática do plugin de segurança
apontou 1 achado (PIN fixo), corrigido.

## Resumo

| Sev. | Achado | Situação |
|------|--------|----------|
| ALTO | PIN provisório fixo (123456) na aprovação do auto-cadastro | CORRIGIDO: `sortear_pin()` por motorista, mostrado uma vez ao aprovador |
| MÉDIO | `POST /api/cadastro` e `/documentos` são públicos (sem login): enxurrada de pendentes, fotos de 10 MB e avisos no WhatsApp | MITIGADO: freio em memória (5 criações/h por IP, 40/dia; só criação conta), aviso só quando CNH+CRLV chegam, 1 vez por cadastro |
| BAIXO | Chave PIX e placa do motorista vão no texto do WhatsApp do grupo ALERTAS FRESH | ACEITO: grupo interno da operação; decisão do Hugo de "valer na hora com aviso" |
| BAIXO | `POST /api/cadastro` diz se o CPF já tem login (enumeração) | ACEITO: a mensagem é necessária pro motorista entender o que fazer; CPF de 11 dígitos sem rate-limit de leitura seria o vetor, mas o freio também limita tentativas por IP |
| BAIXO | Fotos validadas só por extensão | igual aos comprovantes já aceitos; servidas inline só pro nível total do painel |

## O que foi conferido
- **Autorização.** `PUT /api/eu` exige token e só altera o CPF do próprio token; passa
  pelo gate do PIN provisório (403). Rotas do painel: `@requer_auth(niveis=("total",))`
  + `@exige_mesma_origem` nas que escrevem. Documento do cadastro só pro total.
- **Documentos sem login.** O envio exige `chave_envio` (token_urlsafe de 24 bytes, só
  devolvido a quem criou o cadastro), comparado com `hmac.compare_digest`; cadastro já
  avaliado recusa (409). Nome do arquivo é gerado no servidor (`cnh.jpg`/`crlv.png`),
  nunca o nome enviado; `caminho_documento` só aceita `cnh|crlv` e confere `is_file`.
- **Validação.** Telefone 10-11 dígitos (tira +55), e-mail regex, placa `ABC1234`/`ABC1D23`,
  PIX ≤120, zonas/dias só da lista, tipo de veículo pelo catálogo, nome com sobrenome.
  SQL só com parâmetros; o único `f-string` em SQL usa nome de coluna vindo de
  `CAMPOS_EDITAVEIS`/`DOCUMENTOS` (constantes), nunca de entrada.
- **Planilha.** `atualizar_contato_planilha` lê com `dtype={"CPF_MOTORISTA": str}` (armadilha
  do zero à esquerda), só toca nas colunas informadas, best-effort (falha vira warning,
  a alteração no app já está gravada). `cadastrar_motorista` (aprovação) recusa agent_id
  repetido.
- **Aprovação.** Exige CNH+CRLV, agente Vuupt, zonas/dias/tipo válidos; CPF já com login
  recusa; login nasce com `trocar_pin=1` (gate da troca + PIN forte na troca).
- **Histórico.** `motoristas_alteracoes` guarda de/para de cada campo (auditoria do PIX).

## Testes
`nucleo/test_cadastro_motorista.py` (6: validação, gate, duplicidade, chave errada 404,
extensão 400, freio 429, aviso 1x, aprovar/recusar) e
`painel_agentes/test_cadastros_motoristas.py` (3: tela só pro total, documento só pro
total, aprovar/recusar com níveis). Não coberto: `atualizar_contato_planilha` com xlsx
real (só o mock); textos do WhatsApp.
