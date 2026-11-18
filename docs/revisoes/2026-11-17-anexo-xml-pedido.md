# Revisão de segurança: XML da NF-e como anexo do pedido (portal)

Data: 17/11/2026. Ramo `anexo-xml-pedido` contra `origin/master` (c7dc23d).
Arquivos: `portal_cliente/anexos_pedido.py`, `portal_cliente/app.py`,
`portal_cliente/templates/_envios.html`, `portal_cliente/test_anexos_pedido.py`.
Profundidade: triagem focada (mudança pequena, módulo exposto na internet).

## Resultado

Nenhum achado ALTO ou MÉDIO. Três pontos BAIXOS, nenhum bloqueia o deploy.

## O que foi conferido

| Ponto | Conclusão |
|---|---|
| Autenticação e autorização das rotas | Inalteradas: `/api/envios/anexos/ler`, `/anexos` e o download seguem com `@requer_cliente`, `@exige_mesma_origem` (nas de escrita), `_exige_pode_enviar` e `_envio_pra_anexo` (404 pra pedido de outro embarcador). |
| Token temporário do anexo XML usado como pedido no `confirmar` | O arquivo temporário agora pode ser `.xml`, a mesma extensão do XML de pedido. Passar esse token ao `confirmar` cai em `criar_envios`, que relê o XML e roda `validar_item` (emitente tem que ser o embarcador; no grupo, `_contexto` recusa CNPJ fora do acesso). Equivale a subir o mesmo XML como pedido: sem ganho pro atacante. |
| Token de pedido usado como anexo | Recusado: `_ler_temporario` exige o `.meta` com o `dono`, que só o anexo tem. |
| XML aberto no navegador (XSS por XML/XHTML) | Download do XML sai com `as_attachment=True` e `nosniff`; nunca é renderizado na origem do portal. Coberto por teste. |
| Nome do arquivo gravado | Mesma sanitização dos PDFs (`[^A-Za-z0-9._-]` vira `_`, prefixo hash + id do envio). Sem travessia de pasta. |
| Vazamento entre clientes | A checagem de documento repetido continua restrita ao próprio embarcador. O XML não entra em `documentos_processados`, então não mexe no índice NF→pedido nem no romaneio de ninguém. |
| Limites | 10 MB por XML, 30 arquivos por requisição, 30 MB por requisição (Flask), 10 documentos e 1 XML por pedido. |

## Pontos baixos

1. **Leitor de XML (ElementTree) numa rota a mais.** O mesmo `ler_nfe` já lê o XML de pedido em `/api/envios/analisar`; a rota de anexos não abre classe nova de risco, só mais uma entrada pro mesmo leitor. Exige login com permissão de envio.
2. **Emitente do XML anexado não é comparado com o embarcador do pedido.** Hoje o XML só fica guardado e o usuário marca a conferência. **Quando a próxima etapa usar esse XML (Stokki, e-mail, transportadora), a validação de emitente tem que entrar antes.**
3. **Arquivo `.pdf` com conteúdo XML no lote da prévia** passa a ser aceito como XML anexo (antes era recusado como "não é um PDF"). Continua exigindo NF-e válida; inofensivo.

## Cobertura e limites

Testes: `test_anexos_pedido` 30/30 (3 novos). Não houve teste em navegador nem
passagem pela bancada de homologação (decisão do Hugo: direto pra produção).
