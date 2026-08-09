# Configurar o Google Cloud Storage — Agente de Documentos

Passo a passo pra criar o bucket e a conta de serviço do zero (confirmado
com o Hugo, 05/08: não reaproveita nada do agente de documentos anterior).

## 1. Criar (ou escolher) um projeto no Google Cloud

Acesse [console.cloud.google.com](https://console.cloud.google.com/) e crie
um projeto novo (ou use o mesmo já usado pra chave do Google Maps, se
preferir manter tudo junto — não tem problema, são produtos diferentes).

## 2. Ativar a API do Cloud Storage

Menu → **"APIs e Serviços" → "Biblioteca"** → procura **"Cloud Storage API"**
→ **Ativar**.

## 3. Criar o bucket

Menu → **"Cloud Storage" → "Buckets"** → **"Criar"**:
- Nome: precisa ser único globalmente (ex: `freshlog-documentos-pedidos`)
- Região: escolha uma perto do Brasil (ex: `southamerica-east1` — São Paulo)
- Classe de armazenamento: **Standard** (acesso frequente)
- Controle de acesso: **Uniforme** (mais simples)
- Proteção contra exclusão pública: deixe marcado (não precisamos de acesso
  público a esses documentos)

Anota o **nome do bucket** — vai no `config.yaml`.

## 4. Criar a conta de serviço (credencial que o agente vai usar)

Menu → **"IAM e Admin" → "Contas de serviço"** → **"Criar conta de serviço"**:
- Nome: `agente-documentos-pedidos` (ou o que preferir)
- Papel: **Storage Object Admin** (permissão de ler/escrever objetos no
  bucket — não precisa de acesso administrativo ao projeto inteiro)

Depois de criada, clica na conta → aba **"Chaves"** → **"Adicionar chave"**
→ **"Criar nova chave"** → formato **JSON** → baixa o arquivo.

**Guarda esse arquivo `.json` num lugar seguro** (ex:
`C:\agente_stokki_eventos\dados\gcs-service-account.json`) — ele é a senha
de acesso ao bucket. Não sobe pro Git nem compartilha por e-mail.

## 5. Configurar no `config.yaml`

Adiciona essa seção:

```yaml
gcs:
  bucket_name: "freshlog-documentos-pedidos"
  credenciais_json: "C:\\agente_stokki_eventos\\dados\\gcs-service-account.json"
```

(troque o nome do bucket e o caminho pelos que você escolheu/baixou)

## 6. Instalar a biblioteca Python

```
py -3.11 -m pip install google-cloud-storage --break-system-packages
```

## 7. Testar

```
cd C:\agente_stokki_eventos\documentos_pedido
py -3.11 processar_documentos.py --modo-teste
```

Em modo teste, ele mostra o que enviaria (sem enviar de verdade) — depois
que confirmar que está classificando/casando certo, roda sem a flag pra
valer.
