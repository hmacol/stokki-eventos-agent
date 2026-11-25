// Aviso "tem versao nova do app": compara a versao do APK instalado
// (Updates.runtimeVersion = version do app.json, politica appVersion) com a
// versao atual publicada pela API (GET /app-versao, sem login). Se a do
// aparelho for menor, mostra uma faixa no topo que abre o link de download.
// Erro de rede ou Expo Go (runtimeVersion null) -> nao mostra nada.
import React, { useEffect, useState } from 'react';
import { AppState, Linking, Pressable, Text } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import * as Updates from 'expo-updates';
import * as api from './api';
import { cores } from './tema';

/** true se a < b, comparando "1.1.1" x "1.2.0" por partes numericas. */
export function versaoMenor(a: string, b: string): boolean {
  const pa = a.split('.').map((n) => parseInt(n, 10) || 0);
  const pb = b.split('.').map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] ?? 0;
    const y = pb[i] ?? 0;
    if (x !== y) return x < y;
  }
  return false;
}

export function AvisoVersao(): React.JSX.Element | null {
  const [link, setLink] = useState<string | null>(null);
  const insets = useSafeAreaInsets();

  useEffect(() => {
    const instalada = Updates.runtimeVersion;
    if (!instalada) return; // Expo Go / desenvolvimento
    let vivo = true;
    const checar = () => {
      api.appVersao().then(
        (r) => {
          if (vivo) setLink(r.versao_atual && versaoMenor(instalada, r.versao_atual) ? r.link : null);
        },
        () => {}, // sem rede: nada aparece
      );
    };
    checar();
    const sub = AppState.addEventListener('change', (st) => {
      if (st === 'active') checar();
    });
    return () => {
      vivo = false;
      sub.remove();
    };
  }, []);

  if (!link) return null;
  return (
    <Pressable
      onPress={() => void Linking.openURL(link).catch(() => {})}
      style={{ backgroundColor: cores.alerta, paddingTop: insets.top + 8, paddingBottom: 8, paddingHorizontal: 12 }}
    >
      <Text style={{ color: '#fff', fontWeight: '700', textAlign: 'center' }}>Tem versão nova do app. Toque pra baixar.</Text>
    </Pressable>
  );
}
