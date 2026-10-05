# Vigia de pedidos abertos

Criado em 28/09/2026 a pedido do Hugo: "temos tido muitos problemas com
pedidos atrasados ou esquecidos ou parados que não voltam para a rota —
resolver de uma vez por todas".

## Por que existe

O levantamento de 28/09 achou mais de 30 pontos em que um pedido saía do
fluxo sem aviso (entrada Stokki → Vuupt, pool/roteirização, retorno de
insucesso). Corrigir um por um não basta: aparecem outros. O vigia é a
rede de baixo — não importa por onde o pedido escapou, se ele está aberto
há mais tempo do que devia, alguém fica sabendo.

## Estados e prazos (decididos pelo Hugo, 28/09)

| Estado | Quando | Prazo |
|---|---|---|
| Sem serviço | aberto na Stokki, nenhum serviço vivo na Vuupt | 4h |
| No pool | serviço sem rota | 1 dia útil |
| Aguardando o embarcador | pedimos data de agendamento e não veio | 2 dias úteis |
| Rascunho não enviado | em rascunho da data D | 19h do último dia útil antes de D (só alerta) |
| Rascunho com erro | rascunho em ERRO_ENVIO | na hora |
| Preso em rota de dia anterior | rota de ontem pra trás não terminou | na hora |
| Insucesso sem reentrega | sem R1, sem resposta, sem tratar na Torre | 1h depois da 1ª rodada da expedição passadas 12h |
| Embarcador recusou | respondeu "não reenviar" e o pedido segue aberto na Stokki | 2 dias úteis |
| Agendado / Em rota | data futura / rota de hoje em diante | sem prazo |

Dia útil = segunda a sexta e não feriado, pelo calendário único
`regras/calendario.py` (desde 05/10; antes feriado contava como dia útil).

## Peças

- `vigia/regras.py` — estados e prazos, puro.
- `vigia/vigiar.py` — job de 15 min (`stokki-vigia-pedidos`). Só lê o banco:
  `nucleo_pedidos` (espelho da Vuupt de 15 min), `vigia_stokki_abertos`,
  rascunhos, `nucleo_rotas`, `agendamentos_pedido`, fingerprints de insucesso,
  `torre_excecoes_tratadas`. Grava `vigia_pedidos` e `vigia_historico`.
- Retrato da Stokki: o `pipeline.py` grava no fim de toda rodada sem filtro
  quem está aberto e o que fez com cada um (a ação dele vira o motivo do
  "sem serviço": erro, aguardando_redespacho, pulado_cancelado_vuupt...).
  Rodada com fonte falhando é "incompleta" e não fecha ninguém.
- Torre: uma exceção "Vigia" por estado com prazo vencido, link pra `/vigia`.
- Tela `/vigia`: lista pedido a pedido, filtro por estado/vencidos/busca.

## Ações automáticas que vieram junto (28/09)

1. **Reentrega automática** (`expedir_pedidos.reentregar_insucessos_sem_resposta`):
   insucesso sem resposta do embarcador em 12h vira reentrega. NÃO age se:
   - o embarcador respondeu, alguém tratou na Torre, já tem R1/agendamento
     (inclusive R1 feita à mão na Vuupt, vista pelo espelho do núcleo);
   - motivo avaria, validade, manutenção, não coletado, cliente não
     reconheceu, problema fiscal (`MOTIVOS_SEM_REENTREGA_AUTO`) ou motivo que
     não está no de-para;
   - o pedido não está aberto na Stokki (ou o retrato do vigia está velho);
   - retirada, serviço com mais de um pedido ("PS-1, PS-2"), a partir da -R2;
   - insucesso concluído antes de 29/09 ou há mais de 48h (sem rajada no deploy).
   Trava da página de resposta pega e solta POR PEDIDO. Resposta que chega
   depois continua valendo ("não reenviar" cancela a R1 criada).
2. **Rota de dia anterior** (`cancelar_rotas_sem_motorista.py`, 17h45): pedido
   NÃO iniciado volta pro pool antes da roteirização das 18h. Iniciado
   (on_route/arrived) não é mexido: pode ter sido entregue sem baixa — vai
   pro e-mail como "CONFERIR". Rota com movimento HOJE (ainda rodando) não é
   mexida: a carga está no caminhão.
3. **Pipeline de hora em hora** (`stokki-pipeline-horario`, seg-sex 08h05-16h05):
   espera a vez na sessão da Stokki (`stokki/sessao_uso.py`) e desiste se
   continuar ocupada. A expedição e o pipeline das sequências (18h/22h) passam
   a segurar a mesma trava quando está livre. Limite conhecido: rodando pelo
   painel, a trava não é gravada (o painel já conta como "em uso").
4. **Reentrega com caixas**: a R1 nascia sem `dimension_3`.

## Deploy

```
cp infra/stokki-vigia-pedidos.* infra/stokki-pipeline-horario.* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now stokki-vigia-pedidos.timer stokki-pipeline-horario.timer
systemctl restart painel-agentes
sudo -u www-data venv/bin/python -m vigia.vigiar --resumo   # confere antes de gravar
```

O vigia só mostra "sem serviço" depois da primeira rodada do pipeline com o
código novo (é ela que grava o retrato).

## Robustez do retrato (revisão de 28/09)

- Pedido só sai do retrato depois de faltar em DUAS listagens completas (uma
  fonte pode voltar vazia sem erro); reaparecer zera e mantém o "desde".
- Sem retrato completo há 30h, o vigia congela "sem serviço"/insucesso como
  estavam (não apaga) e a Torre mostra "Vigia sem retrato recente da Stokki".
- Retirada conta como serviço vivo (mesmo código do pedido).

## Pendências conhecidas

- ROTA_PASSADA de rota com mais de 14 dias não é devolvida sozinha (só
  alertada); rodar `--resumo` antes de ligar pra ver o volume.

- O retrato cobre o que o pipeline lista: "Aguardando Transportador" de
  todos + todos os status dos 3 prioritários + Estação de Impressão. "On hold"
  de outros embarcadores não entra.
- `enviar_rascunhos_pendentes.py` segue sem timer (decisão: só alerta).
