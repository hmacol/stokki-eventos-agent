# Notificação de Transportadoras com XML da NF-e

Criado em 13/08/2026 a pedido do Hugo: *"criar rotina para notificar com
XML, transportadoras quando tivermos entregas para eles [...] identificar
os pedidos que vão para esses locais e disparar notificação para
transportadora com cópia para o remetente do XML das notas a serem
entregues no dia"*.

Roda em `notificacao_transportadoras\notificar_transportadoras.py`.

## O que ele faz

1. **Busca** as rotas do dia no VUUPT (`buscar_rotas_do_dia`, mesma fonte
   de `gerar_pdf_romaneios.py`/`avisar_motoristas_rotas.py`).
2. **Bate o endereço** do serviço na VUUPT (campo `address`) contra os
   pontos de redespacho da planilha (`regras/transportadoras.py::
   resolver_por_endereco`): CEP + número do imóvel; se não casar, rua
   (sem R./AV./ESTRADA) + número + município. **Mudança de 10/09 (pedido
   do Hugo):** o critério deixou de ser o bloco "Transportadora" da
   Stokki, porque o cliente às vezes esquece de informar a transportadora
   lá, mas o endereço de entrega já é o do galpão (caso #PS-38850, TAFF).
   Assim tanto o pedido que veio pelo redespacho automático quanto o que o
   cliente digitou o endereço do galpão à mão entram.
   O bloco "Transportadora" da Stokki continua sendo consultado
   (`conferir_na_stokki`), mas só como **conferência informativa**: se
   divergir (sem transportadora, outra, desconhecida) vai pro log e pro
   resumo interno, nunca trava o envio; o e-mail de contato de lá vira
   fallback quando a planilha não tem e-mail pro ponto.
3. **Agrupa** os pedidos por **ponto de redespacho** (galpão — KANEJO e
   IMG são a mesma Rua Osaka 880; CENTROSUL, ANDREA BELOTTO, FREZZE e
   GESSY LOPES a mesma Rua Makita Brasil 300) e descarta os que já foram
   notificados hoje (rate-limit incremental — ver abaixo).
4. **Baixa o XML** cru da NF-e de cada pedido novo, direto da aba
   Documentos do pedido na Stokki (Playwright — `documentos_pedido/
   stokki_documentos.py::baixar_xml_nfe`, mesma técnica já usada pra
   gerar o DANFE em PDF, mas pegando o link `/xml/nfe/...` que antes era
   ignorado de propósito).
5. **Envia 1 e-mail por transportadora**: `To` = e-mail da transportadora,
   `Cc` = e-mail(s) do(s) embarcador(es) (remetentes) dos pedidos do
   grupo, com todos os XMLs anexados.

## Como identifica "transportadora" e resolve os e-mails

- **Ponto de redespacho**: endereço (colunas C-I) das linhas tipo
  **TERCEIROS** da planilha `BD_TRANSPORTADORAS.xlsx`. Linhas com o mesmo
  CEP + número viram 1 ponto só; a transportadora "dona" do ponto (nome
  exibido no e-mail e chave do fingerprint) é a que aparece no complemento
  da planilha (`Rua Osaka 880 KANEJO` → KANEJO), senão a primeira com
  e-mail, senão a primeira linha. Linha sem número (ou sem CEP e sem
  rua/município) fica fora do batimento.
- **E-mail da transportadora**: coluna **T** da planilha, união de todas
  as linhas do ponto (aceita vários, separados por vírgula/;/quebra de
  linha). Se vier vazia, cai no fallback do e-mail de contato cadastrado
  no próprio Stokki para aquele pedido (`detalhe["transportadora"]["email"]`).
  Sem nenhuma das duas fontes, o grupo inteiro fica sem notificação
  (contabilizado no resumo, tenta de novo na próxima execução).
- **E-mail do(s) embarcador(es) (Cc)**: `sender_id` de cada pedido →
  tabela `interno` (mesmo `_carregar_embarcadores_por_sender_id()` de
  `roteirizacao/notificar_agendamento_dia_fixo.py`), deduplicado entre
  todos os pedidos do grupo.

## Rate-limit (pedido do Hugo, 13/08 — incremental, não "1x e pronto")

`fingerprint_notificacao_transportadora.py` grava 1 linha por
`(pedido, transportadora)` em `dados/dados.db` quando o e-mail sai com
sucesso. Um pedido já notificado hoje pra aquela transportadora não entra
de novo na mesma execução nem em execuções seguintes do mesmo dia — mas
se um pedido **novo** for roteirizado pra mesma transportadora mais tarde,
ele gera um e-mail adicional só com os itens ainda não enviados (mesmo
padrão de `redespacho_confirmacao.py`).

## Arquivos

| Arquivo | Papel |
|---|---|
| `notificacao_transportadoras\notificar_transportadoras.py` | Script principal |
| `notificacao_transportadoras\fingerprint_notificacao_transportadora.py` | Tabela `notificacoes_transportadora_enviadas` — rate-limit incremental por `(pedido, transportadora)` |
| `documentos_pedido\stokki_documentos.py` (`localizar_link_xml_nfe`, `baixar_xml_nfe`) | Download do XML cru da NF-e por pedido |
| `regras\transportadoras.py` (`ResultadoResolucao.email`) | E-mail da transportadora, coluna T da planilha |
| `email_utils.py` (`cc=`, `anexos=` em `enviar_email`) | Suporte a cópia e anexos genéricos, reaproveitado por qualquer notificação futura |
| `criar_tarefa_notificar_transportadoras.ps1` | Cria a tarefa agendada (rodar como admin) |
| `rodar_notificar_transportadoras.ps1` | Wrapper chamado pela tarefa agendada |
| `dados\notificar_transportadoras.log` | Log do script |
| `dados\BD_TRANSPORTADORAS.xlsx` (coluna T, nova) | E-mail de cada transportadora |

## Execução

```
py -3.11 notificacao_transportadoras\notificar_transportadoras.py                       # real, hoje
py -3.11 notificacao_transportadoras\notificar_transportadoras.py --modo-teste          # não envia de verdade, não marca fingerprint
py -3.11 notificacao_transportadoras\notificar_transportadoras.py --data 15/08/2026     # data específica
```

Em `--modo-teste`, todo e-mail é redirecionado (sem Cc) para
`hugo@freshlogbr.com`, com um bloco "MODO TESTE" no topo mostrando o
destino/Cc originais, o ponto de redespacho e o que a conferência na
Stokki achou por pedido — e o modo de teste **ignora a chave-mestra**
`notificacoes_automaticas.ativo` (que só bloqueia envio real), senão não
dá pra ensaiar com ela desligada. Na VPS: `sudo -u www-data venv/bin/python
notificacao_transportadoras/notificar_transportadoras.py --modo-teste --data hoje`.

Agendamento: tarefa `StokkiEventos_NotificarTransportadoras`, diária às
**04:10** — 10 min depois de `StokkiEventos_RomaneiosManha` (04:00), só
por precaução de concorrência entre os dois Playwrights (não há
dependência real de dados entre eles; a única pré-condição real é que as
rotas do dia já estejam montadas, o que `StokkiEventos_SequenciaNoite`
(22h da véspera) já garante). Criar com
`.\criar_tarefa_notificar_transportadoras.ps1` (admin).

## Decisões de desenho

- **Identificação por reconfirmação na Stokki, não pelo endereço do
  serviço no VUUPT**: o endereço de um pedido TERCEIROS já foi substituído
  pelo endereço fixo de redespacho da planilha — dá pra saber que o
  destino é "de uma transportadora" só olhando o endereço, mas não *qual*
  transportadora, nem confiar em correspondência de string de endereço
  formatado. Reconfirmar via `catalogo.resolver()` reaproveita exatamente
  a mesma regra que `pipeline.py` já usa, sem depender de heurística nova.
- **XML direto da Stokki, não do pipeline de importação por e-mail**
  (`C:\agente_importacao_stokki`): esse pipeline só cobre 1 cliente
  (Empório Quatro Estrelas) e tem um caminho quebrado no `config.yaml`
  (`importacao_email.pasta_xmls_processados` aponta pra uma pasta que não
  existe). Buscar o XML por pedido direto na Stokki funciona pra qualquer
  cliente/embarcador, sem depender desse outro pipeline.
- **Playwright só para os pedidos que sobraram do rate-limit**: a
  identificação (via `requests`, barata) roda pra todos os pedidos do
  dia; o download do XML (Playwright, mais caro) só roda pros pedidos que
  realmente vão gerar e-mail novo.
- **Pedido sem XML anexado na Stokki** não trava o grupo: é descartado da
  lista de itens (contabilizado em "sem_xml" no resumo) e tentado de novo
  na próxima execução (não marca fingerprint pra ele).

## Pendências / próximos passos

- **Preencher a coluna E-mail (T)** em `dados\BD_TRANSPORTADORAS.xlsx`
  para as transportadoras TERCEIROS — sem isso, a rotina depende do
  fallback (e-mail de contato cadastrado no Stokki), que pode não estar
  preenchido pra todas.
- Primeira execução real (`--modo-teste` e depois um dia real) ainda não
  validada em produção — rodar manualmente antes de confiar na tarefa
  agendada.
- `importacao_email.pasta_xmls_processados` (caminho quebrado no
  `config.yaml`) segue como achado registrado, fora do escopo desta
  rotina — não corrigido aqui.
