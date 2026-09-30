# Calculadora de frete dedicado no portal do cliente

Pedido do Hugo, 11/09/2026: o cliente cota online um veículo dedicado
(Fiorino, Van/HR ou VUC) saindo do galpão até os endereços dele, recebe a
proposta em PDF por e-mail com botão de aceite e a equipe recebe o aceite.
A tabela padrão das regiões atendidas ficou **em pausa** (decisão do Hugo).

## Onde está

| Arquivo | O quê |
|---|---|
| `portal_cliente/cotacao.py` | regras (`calcular`, `escolher_veiculo`, `km_excedente`), CEP (ViaCEP/BrasilAPI), lista .xlsx/.csv, geocodificação + km, tabela `portal_cotacoes`, token de aceite, e-mails |
| `portal_cliente/cotacao_pdf.py` | PDF da proposta (Pillow, mesmo padrão dos romaneios) |
| `portal_cliente/cotacao_web.py` | rotas Flask (`/cotacao`, `/api/cotacao/*`, `/cotacao/aceite/<token>`) |
| `portal_cliente/templates/cotacao.html`, `cotacao_aceite.html`, `_tour_cotacao.html` | tela, página pública de aceite, guia da tela |
| `roteirizacao/km_rodoviario.py` | `calcular_trajeto()` novo: km (ida, volta e total) + pedágio (`extraComputations: TOLLS`) |
| `config.yaml` → `portal_cliente.cotacao` | tabela de veículos, percentuais, origem, e-mails, `forcar_destino` |
| `portal_cliente/test_cotacao.py` | 22 testes (`py -3 -m pytest portal_cliente/test_cotacao.py -q`) |

## Regras (decisões do Hugo, 11/09)

- Veículo escolhido automaticamente: o menor que comporta **caixas e peso**.
  Fiorino 100 cx/550 kg · Van/HR 400 cx/1.300 kg · VUC 600 cx/2.000 kg.
  Acima disso: "solicite cotação" (fica registrado como FORA_DA_TABELA).
  O Hugo avisou (11/09) que a capacidade em caixas **varia muito com o
  tamanho da caixa** — a tela diz isso ao cliente, mas a conferência real
  é da operação.
- Base: Fiorino R$ 650 até 65 km (+R$ 2/km) · Van/HR R$ 850 até 100 km
  (+R$ 2/km) · VUC R$ 1.300 até 120 km (+R$ 2,50/km).
- Carga seca: R$ 150 a menos na saída. Refrigerado = congelado.
- Km: Google Routes, galpão → entregas na ordem → **galpão** (o retorno
  sempre conta, Hugo 11/09; `considerar_retorno` no config desliga);
  excedente em km inteiro arredondado pra cima.
- Ad valorem 0,5% da NF (campo opcional). Same-day +40% sobre frete + ad valorem.
- Impostos 12% por gross-up (total = líquido / 0,88). Pedágio fora do gross-up,
  estimado pela Routes API e cobrado pelo valor real.
- Proposta vale 7 dias. Aceite pelo portal ou pelo link assinado do e-mail
  (itsdangerous, sem login). Aceite abre chamado no atendimento (área
  "Coleta / retirada", AGUARDANDO_FL) e avisa `email_comercial` com o PDF.

## O que o cliente vê (Hugo, 11/09 — 2ª rodada)

- **E-mail da proposta = objetivo**: número da cotação, validade, o **valor
  final** numa caixa e o botão **Aprovar proposta**. Sem lista de entregas e
  sem composição — o detalhamento vai no PDF anexo.
- **PDF = detalhado**, mas a composição do valor tem só 3 ou 4 linhas:
  1. `Frete dedicado <veículo> · <km> km (ida e volta) · pedágio incluso`
     — frete base **menos** desconto de carga seca, **mais** km adicional,
     **mais** pedágio, tudo já somado (`resultado.frete_com_pedagio`);
  2. `Ad valorem` (separado);
  3. `Urgência same-day` (só quando marcada);
  4. `Impostos` (separado).
- **Km e pedágio são sempre de IDA E VOLTA** ao galpão: a Routes API é chamada
  com o retorno dentro do trajeto (`voltar=True`), então a estimativa de
  pedágio já é do percurso inteiro — não é o pedágio da ida dobrado.
- A composição fica num lugar só: `cotacao.linhas_composicao(r)`, usada pelo
  PDF e pelos e-mails; a tela repete a mesma lógica em JS (cotacao.html).
  `detalhado=True` abre base/desconto/km/pedágio e é usado **só** no e-mail
  interno do aceite (comercial precisa ver a abertura).
- Vocabulário pro cliente virou "aprovar / aprovada" (e-mail, página do link e
  botões da tela); o status no banco continua `ACEITA`.

## Premissas: todas confirmadas pelo Hugo (11/09)

1. ~~Fiorino = 200 caixas~~ → **100 caixas** (Hugo: "varia muito do tamanho delas").
2. Ad valorem entra no gross-up dos 12% (compõe o frete); pedágio não. ✔
3. Same-day incide sobre frete + ad valorem, antes do gross-up. ✔
4. ~~Km só de ida~~ → **sempre considerar o retorno** ao galpão (km e pedágio).
5. Caixas e peso são totais da cotação, não por entrega. ✔

### 2ª rodada: confirmadas pelo Hugo (12/09)

6. **Urgência same-day em linha separada** no PDF (não entra na linha do
   frete). ✔
7. **Desconto de carga seca invisível** pro cliente — entra abatido dentro da
   linha do frete, sem linha própria em lugar nenhum. ✔
8. **Valor do pedágio embutido na linha do frete** — o cliente nunca vê o
   valor estimado separado; as condições só dizem que está incluso e que é
   cobrado pelo valor real. ✔

## Piloto

`portal_cliente.cotacao.forcar_destino: hugo@freshlogbr.com` — enquanto
preenchido, TODO e-mail da cotação (proposta, aceite, confirmação) vai só
pro Hugo. Esvaziar (`''`) pra liberar aos clientes.

## Deploy

`git pull` na VPS + `systemctl restart portal-cliente`. Precisa da seção
`portal_cliente.cotacao` no config.yaml da VPS (copiar do local). A Routes
API precisa aceitar `extraComputations: TOLLS` (mesma chave; se falhar cai
em linha reta sem pedágio e a tela avisa "linha reta").
