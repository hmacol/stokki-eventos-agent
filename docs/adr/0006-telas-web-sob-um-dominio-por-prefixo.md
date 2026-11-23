# ADR-0006: Telas web sob `app.freshhub.com.br/<serviço>` via Caddy + ProxyFix + `url_for`

## Status
Aceito

## Data
2026-07 (migração para a VPS)

## Contexto
Cinco apps Flask separados (painel interno, portal do cliente, API do motorista, página de insucesso, confirmação de rota) rodam cada um em waitress numa porta local (8071-8074, 8090, 8095). Precisam de HTTPS, um só certificado e URLs estáveis para links em e-mail, WhatsApp e no app Expo.

## Decisão
- Um guarda-chuva `app.freshhub.com.br` no Caddy; cada serviço em um prefixo: `/painel`, `/cliente`, `/motorista`, `/insucesso`. Exceções com subdomínio próprio por terem deploy separado: `atendimento.` e `confirmacao.`.
- Caddy usa `handle_path` (tira o prefixo) e o Flask usa `ProxyFix(x_prefix=1)` para reconstruir URLs.
- **Templates usam sempre `url_for()`; nunca caminho absoluto.** Caminho absoluto quebra atrás do prefixo.
- Cada app tem o próprio bloco Caddy em `Caddyfile-*` dentro da pasta do serviço, junto da unit systemd.

## Alternativas consideradas

### Um subdomínio por serviço
- Prós: sem ProxyFix, sem prefixo.
- Contras: um DNS e um certificado por tela; links já distribuídos.
- Usada só onde o deploy é separado (atendimento, confirmação).

### Um só app Flask com blueprints
- Rejeitada: os serviços têm ciclo de deploy e nível de risco diferentes (reiniciar o portal não deve derrubar a API do motorista).

## Consequências
- Tela nova = unit systemd + bloco Caddy + `ProxyFix` + `url_for` em todo template. Testar localmente em outra porta (`8099`), nunca na 8070 do painel real.
- Deploy reinicia **só** o serviço que importa o módulo alterado; rotinas em lote pegam a versão nova sozinhas.
- O app Expo e os links assinados (canhoto, insucesso, aceite) dependem desses prefixos; mudar prefixo é mudança incompatível.
