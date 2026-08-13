# Romaneios em PDF — 1 PDF por rota com NFs e boletos

Pedido do Hugo, 11/08/2026: "1 documento PDF por romaneio criado, com
todas notas fiscais e boletos, sempre nessa ordem, por pedido,
aproveitando a documentação que já exportamos".

## Objetivo

Gerar, de manhã cedo (antes da saída dos motoristas), **1 PDF por
romaneio** — romaneio = rota do VUUPT (`Planejamento - DD/MM/AAAA - #N`).
Dentro do PDF, pedido a pedido **na ordem de visita da rota**: todas as
Notas Fiscais do pedido, seguidas de todos os Boletos.

## Fontes de dados (nada é baixado do GCS)

| Fonte | O quê |
|---|---|
| API VUUPT (`listar_rotas` com `include=services`) | Rotas do dia + pedidos na ordem de visita (`code` = PS-XXXXX) |
| SQLite `dados/dados.db`, tabela `documentos_processados` | Índice dos documentos: tipo, pedido, nº NF, parcela, status |
| `documentos_pedido/dados/{downloads_stokki_temp, boletos_separados, anexos_temp}` | Os PDFs físicos (resolvidos por `documentos_pedido/localizar_arquivos.py`) |
| `dados/BD_MOTORISTAS.xlsx` (CatalogoMotoristas) | Nome do motorista a partir do `agent_id` da rota |

## Regras de seleção

- Só documentos com `status='ENVIADO'` e `tipo` em `Nota Fiscal` /
  `Boleto`. `Outro` (Romaneio Interno, Comprovante de Entrega etc.) e
  `REVISAO_MANUAL` ficam de fora — os em revisão são contados e
  avisados no resumo.
- **NFs**: dedup por nº de NF (pode existir DANFE gerada + nota anexada
  manualmente). Preferência: `origem='stokki'` (DANFE oficial); empate →
  mais recente. NF sem número extraído entra sempre. Ordem: nº da NF.
- **NF dispensada** (pedido do Hugo, 13/08): entregas de **Padrão Puro,
  Quatro Estrelas e Pedramoura** (os mesmos sender_ids da canhoteira)
  não precisam ir acompanhadas de NF — a falta de NF não vira pendência
  e a capa mostra "—" na coluna NF em vez do X vermelho. O agente de
  documentos também **pula a geração da DANFE** desses pedidos na
  Stokki (`documentos_pedido/selecionar_pedidos.py::EMBARCADORES_SEM_NF`).
  Se uma NF antiga já existir no banco, ela ainda é impressa.
- **Boletos**: sem dedup (hash é PK). Ordem: nº NF → nº parcela → data.

## Estrutura do PDF (ajuste do Hugo, 11/08: sem páginas separadoras)

1. **Capa em PAISAGEM** (pedido do Hugo, 13/08) com identidade Freshlog
   (logo `assets/logo_freshlog.png`, azul-marinho + verde-água): data,
   motorista, nº de pedidos e uma **tabela com 1 linha por pedido** na
   ordem de visita — pedido, embarcador (nome curto da tabela
   `interno`), cliente, **endereço de entrega** (campo `address` do
   serviço VUUPT, sem CEP/Brasil), **volumes**, **peso bruto em kg** e
   status de **NF / BOL** com check verde ou X vermelho (pendência).
   Continua em página extra se a rota tiver mais pedidos do que cabe.
   - **Volumes e peso**: extraídos do bloco "Transportador / volumes
     transportados" da **DANFE local** do pedido (regex sobre o texto
     do pypdf; soma quando o pedido tem mais de uma NF). Sem DANFE
     legível, volumes caem pro `dimension_3` do serviço VUUPT dividido
     pelo `interno.fator_ponderado` do embarcador (o `dimension_3` é o
     volume **ponderado** = qtd real × fator, ver `pipeline.py`) e o
     peso fica "—". A canhoteira segue em retrato.
2. **Documentos emendados direto** (sem separadores): NFs e depois
   boletos, pedido a pedido, na ordem de visita.
3. **CANHOTEIRA** (páginas finais, só quando a rota tem entrega de
   **Padrão Puro, Quatro Estrelas ou Pedramoura** — sender_ids
   12887364, 21785428, 21911340): tabela na ordem da rota
   identificando motorista, rota e dia, com 1 linha por entrega
   (parada, pedido, nº NF, embarcador, cliente) e campos em branco de
   Recebedor / Assinatura / Data.

Páginas geradas com Pillow (A4 a 150 dpi, Arial do Windows, checks/X
desenhados) e mescladas com pypdf — sem dependência nova.

## Saída, nomenclatura e idempotência

- Pasta: `roteirizacao/dados/romaneios/{AAAA-MM-DD}/` (gitignorada);
  em modo-teste: `roteirizacao/dados/romaneios/teste/{AAAA-MM-DD}/`.
- Nome: `romaneio_{AAAA-MM-DD}_rota{N}_id{route_id}_{MOTORISTA}.pdf`.
- `_resumo.txt` na mesma pasta: linha por rota + bloco de pendências.
- **Idempotente**: execução completa apaga os `romaneio_*.pdf` da pasta
  da data antes de regerar (remove órfãos de rota renomeada); com
  `--rota`, apaga só os PDFs daquela rota.
- Rota cancelada ou sem serviços é pulada (warning). Erro numa rota
  não derruba as demais (vira etapa "erro" no resumo).

## CLI

```
py -3.11 roteirizacao/gerar_pdf_romaneios.py [--modo-teste] [--data hoje|amanhã|DD/MM/AAAA] [--rota <route_id>]
```

- `--data` padrão: **hoje** (o job roda de madrugada para as rotas do
  próprio dia).
- `--modo-teste`: gera tudo em `romaneios/teste/` e **não** envia a
  notificação de execução.
- Também disparável pelo painel de agentes (id `gerar_romaneios`,
  categoria Roteirização).

## Agendamento

`rodar_romaneios_manha.ps1` (usa o caminho completo `C:\Windows\py.exe`
— workaround do 'py' fantasma em System32). Tarefa **já registrada**
(11/08/2026) no mesmo modo das demais StokkiEventos_* ("Interativo
apenas", roda com o usuário logado — sessão bloqueada serve):

```powershell
schtasks /Create /TN "StokkiEventos_RomaneiosManha" `
    /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\agente_stokki_eventos\rodar_romaneios_manha.ps1" `
    /SC DAILY /ST 04:00
```

Logs: `dados\sequencia_romaneios.log` (resumo),
`dados\RomaneiosManha_sequencia_std{out,err}.log` (saída do script),
`roteirizacao\dados\gerar_pdf_romaneios.log` (log do Python).

## Troubleshooting

- **"ARQUIVO NÃO LOCALIZADO"**: o PDF sumiu das 3 pastas temporárias
  (limpeza manual?). O documento existe no GCS
  (`gs://freshlog-documentos-pedidos/pedidos/{PS}/{Tipo}/`) — baixar de
  lá manualmente ou reprocessar o pedido em processar_documentos.
- **"PDF ILEGÍVEL"**: arquivo corrompido/protegido; abrir o original e
  reprocessar.
- **Fonte estranha na capa**: Arial/Segoe UI não encontradas em
  `C:\Windows\Fonts` — o script cai no bitmap default do Pillow (feio,
  mas funciona).
- **Pedido sem documentos**: normal quando o boleto ainda não chegou —
  a página de AVISO no PDF e o `_resumo.txt` mostram o que faltou.

## Critérios de aceite

- [x] 1 PDF por rota não-cancelada do dia, na pasta da data.
- [x] Capa com logo/identidade Freshlog e tabela de status NF/BOL por
  pedido; documentos emendados sem páginas separadoras.
- [x] Dedup de NF (stokki preferida) e ordenação de boletos por
  NF/parcela.
- [x] Canhoteira nas páginas finais quando houver entrega de Padrão
  Puro / Quatro Estrelas / Pedramoura, na ordem da rota, com motorista,
  rota e dia no cabeçalho.
- [x] Pendências granulares por pedido (X na capa), no `_resumo.txt` e
  na notificação.
- [x] Idempotência por data e por `--rota`.
- [x] Registrado no painel + tarefa `StokkiEventos_RomaneiosManha` (04h).
