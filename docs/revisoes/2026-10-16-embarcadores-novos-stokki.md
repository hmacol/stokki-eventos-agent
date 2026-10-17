# Review de segurança: pré-cadastro de embarcador novo (deploy 16/10)

Diff: `7e10cc9..origin/master` (24c3e35), parte exposta: `portal_cliente/app.py`,
`portal_cliente/templates/completar_cadastro.html`, `cadastro_embarcador.py`,
`preferencias_notificacao.salvar` (já existente), `painel_agentes/painel_agentes.py`.

## Resultado: nenhum achado ALTO ou MÉDIO

| Ponto | Verificado |
|---|---|
| `/cliente/completar-cadastro` exige sessão | `g.cliente` vem de `auth.sessao_valida`; sem sessão → login. CNPJ sai da sessão, nunca do formulário. |
| Bloqueio do resto do portal até completar | `_carregar_cliente` redireciona (403 em /api) enquanto `precisa_completar`; lista de rotas livres = a da troca de PIN + a própria tela. |
| CSRF no POST | Sem `@exige_mesma_origem` (igual a `trocar_pin`), mitigado por `SESSION_COOKIE_SAMESITE="Lax"`: POST cross-site não leva o cookie. **BAIXO.** |
| Validação | Tipo por `normalizar_habilidade` (lista fechada); e-mails por `_validar_emails`; WhatsApp por `normalizar_whatsapp`; erro não grava nada. Jinja escapa os valores devolvidos ao formulário. |
| Spam no grupo do WhatsApp | Aviso só após POST válido; depois disso a situação vira PREENCHIDO, que não está em `ABERTOS_PRO_CLIENTE`, e a tela redireciona. Um aviso por cadastro. |
| Aprovação | `/api/cadastro-embarcador/<cnpj>/aprovar`: `requer_auth(niveis=("total",))` + `exige_mesma_origem`; sem tipo explícito exige PREENCHIDO; tipo explícito passa por `normalizar_habilidade`. SQL parametrizado. |
| E-mail real ao cliente | `interno.email` só é preenchido em `aprovar()`, então o pipeline não manda confirmação a quem não foi aprovado. |

## BAIXO (sem bloquear)

1. `completar_cadastro` sem `@exige_mesma_origem`. Mesmo padrão de `trocar_pin`; se um dia o cookie deixar de ser Lax, os dois ficam expostos. Sugestão: decorar os dois.

## Limites

Revisão do diff exposto e das funções chamadas por ele. `embarcadores_stokki.py` (rotina interna de timer, sem entrada externa além da Stokki) foi lido só pelos pontos de envio de mensagem.
