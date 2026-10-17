# Review de seguranca: correcao A1/A3 do portal (ramo pin-seguro)

Data: 10/10/2026. Diff: `portal_cliente/app.py`, `portal_cliente/auth_cliente.py` (+ testes).

## O que muda
- PIN padrao (trocar_pin=1) vence 7 dias apos `criado_em`: login com a credencial certa devolve 403 com orientacao; sessao aberta cai.
- `/login`: trava por IP (20 erros de credencial em 15 min -> 429). So erro 401 conta; 429 e 403 nao.
- `/trocar-pin` e `/definir-pin`: `@exige_mesma_origem`. O decorador passou a conferir so POST (o GET do link vem do webmail com Referer de fora). Todas as outras rotas decoradas ja eram POST-only: sem afrouxamento.
- E-mail ao cliente quando o PIN e definido/trocado (nome com html.escape); falha no envio nao impede a troca.

## Achados
- Nenhum ALTO/MEDIO novo.
- BAIXO: o 403 "PIN provisorio venceu" confirma que o CNPJ tem conta com PIN padrao, mas so pra quem ja digitou o PIN padrao certo. Aceito.
- BAIXO: 20 erros por IP em 15 min pode travar uma rede corporativa com muitos usuarios errando. Aceito; a trava por conta (5) continua.

## Efeito no deploy
As 21 contas criadas em 03/10 com o PIN 123456 vencem na hora (mais de 7 dias). Quem nao trocou precisa do link de "Primeiro acesso".

## Testes
`portal_cliente.test_pin_padrao`: 29 OK (16 novos). Suite do portal: 227, 1 erro pre-existente (`test_tela_inicial_tem_o_botao_notificacoes`, falta tabela `interno` no banco de teste; falha igual no origin/master).
