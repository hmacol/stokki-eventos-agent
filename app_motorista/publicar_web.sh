#!/usr/bin/env bash
# Publica o PWA dos motoristas (iPhone) em https://motorista.freshhub.com.br/ (origem própria).
# Roda na máquina do Hugo, dentro de app_motorista/:  bash publicar_web.sh
#
# 1. exporta o web (public/ vai junto: manifest, service worker, ícones)
# 2. envia pra VPS em dados/app_motorista/web.novo e troca de pasta de uma vez
#    (quem está com o app aberto nunca vê a pasta pela metade)
# O Caddy serve motorista.freshhub.com.br dessa pasta (site em infra/Caddyfile-motorista-pwa).
# A pasta fica fora do git, como o APK. Nada aqui mexe em serviço nem em banco.
set -euo pipefail

VPS="root@187.127.52.197"
CHAVE="$HOME/.ssh/atendimento_vps"
DESTINO="/opt/stokki-eventos/dados/app_motorista"

cd "$(dirname "$0")"
rm -rf dist
CI=1 npx expo export --platform web --output-dir dist

# Sem isto o app abriria em branco: o index tem que apontar pra raiz do domínio.
grep -q 'src="/_expo/static/js/web/' dist/index.html || { echo "index.html com caminho inesperado: abortei"; exit 1; }
test -f dist/sw.js && test -f dist/manifest.webmanifest || { echo "faltou sw.js/manifest no export: abortei"; exit 1; }

tar -C dist -cf - . | ssh -i "$CHAVE" "$VPS" "
  set -e
  mkdir -p $DESTINO
  rm -rf $DESTINO/web.novo && mkdir $DESTINO/web.novo
  tar -C $DESTINO/web.novo -xf -
  chown -R www-data:www-data $DESTINO/web.novo
  rm -rf $DESTINO/web.antigo
  [ -d $DESTINO/web ] && mv $DESTINO/web $DESTINO/web.antigo
  mv $DESTINO/web.novo $DESTINO/web
  rm -rf $DESTINO/web.antigo
"
echo "Publicado. Abra https://motorista.freshhub.com.br/ (precisa do DNS e do site no Caddy)."
