# Revisão de segurança: /cliente/aula (PR #21, ramo aula-portal)

Data: 13/11/2026 · Diff: `6f4ba18..aula-portal -- portal_cliente` (app.py, aula.py, aula.html, base_cliente.html, login.html, test_aula.py)

## Superfície nova
- `GET /aula` pública (sem login), lê `dados/aula/aula.json`.
- `GET /aula/arquivo/<nome>` pública, entrega mp4/jpg.
- `aula` e `aula_arquivo` entram em `_LIVRES_ANTES_DA_TROCA` (cliente com troca de PIN pendente pode abrir).

## Pontos checados
| Ponto | Resultado |
|---|---|
| Path traversal em `<nome>` | Só nome EXATO de `video`/`capa` do manifesto passa (`arquivo_permitido`), e o `send_from_directory` ainda barra caminho. `..%2Fconfig.yaml` e `aula.json` dão 404 (teste). |
| Vazamento do manifesto / outros arquivos de `dados/` | `aula.json` não é servido; pasta própria `dados/aula`. |
| XSS | Manifesto entra por `{{ videos \| tojson }}` (escapa `<>&'`) e por `{{ }}` autoescapado. Manifesto é escrito só no deploy (scp), não por usuário. |
| Manifesto quebrado | `carregar_manifesto` devolve lista vazia em JSON inválido, tipo errado ou `numero` não numérico: página mostra "sendo preparados", sem 500 (teste). |
| Dado real nos vídeos | Gravação em banco e config fictícios (CNPJ 47.821.930/0001-70, clientes inventados); Stokki/Vuupt nunca acessadas; rede bloqueada fora de 127.0.0.1. |
| Sessão / auth | Rotas não leem nem gravam sessão; nenhuma ação, só leitura de arquivo estático. |
| Abuso de banda | mp4 de 1–4 MB com `Cache-Control: public, max-age=3600` e Range; mesmo risco de qualquer estático. |

## Achados
- ALTO: nenhum.
- MÉDIO: nenhum.
- BAIXO: `carregar_manifesto` relê o JSON a cada requisição de arquivo (custo desprezível com 12 itens). Sem ação.

Revisão feita à mão (diff pequeno), seguindo o checklist da differential-review.
