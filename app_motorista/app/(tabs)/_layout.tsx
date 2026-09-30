import React, { useEffect, useState } from 'react';
import { AppState, Pressable, StyleSheet, Text, View } from 'react-native';
import type { ColorValue } from 'react-native';
import { Tabs, useRouter } from 'expo-router';
import { Ionicons } from '@expo/vector-icons';
import * as api from '../../src/api';
import { useSessao } from '../../src/sessao';
import { cores } from '../../src/tema';

type Icone = React.ComponentProps<typeof Ionicons>['name'];
const icone = (nome: Icone) => ({ color, size }: { color: ColorValue; size: number }) => <Ionicons name={nome} color={color} size={size} />;

// De quanto em quanto tempo o app confere se a logística respondeu. Chamada
// leve (só um número) -- o chat em si só busca com a tela aberta.
const INTERVALO_NAO_LIDAS_MS = 60000;

export default function Abas() {
  const [naoLidas, setNaoLidas] = useState(0);
  const { motorista } = useSessao();
  const router = useRouter();

  useEffect(() => {
    let vivo = true;
    const conferir = () => {
      if (!api.temSessao()) return;
      api.atendimentoNaoLidas().then((r) => { if (vivo) setNaoLidas(r.nao_lidas); }).catch(() => {});
    };
    conferir();
    const t = setInterval(conferir, INTERVALO_NAO_LIDAS_MS);
    // Voltar pro app é o momento mais provável de ter resposta esperando.
    const sub = AppState.addEventListener('change', (e) => { if (e === 'active') conferir(); });
    return () => { vivo = false; clearInterval(t); sub.remove(); };
  }, []);

  // Perfil no alto (Hugo, 13/09): o nome do motorista fica à vista em toda
  // aba e o toque abre a tela de perfil -- saiu da barra de baixo.
  const perfilNoTopo = () => {
    const partes = (motorista?.nome ?? '').replace(/\(.*?\)/g, '').trim().split(/\s+/).filter(Boolean);
    const primeiro = partes[0] ?? 'Perfil';
    const iniciais = ((partes[0]?.[0] ?? '') + (partes.length > 1 ? partes[partes.length - 1][0] : '')).toUpperCase() || '?';
    return (
      <Pressable onPress={() => router.push('/perfil')} hitSlop={8} style={s.perfil}
        accessibilityRole="button" accessibilityLabel={`Perfil de ${motorista?.nome ?? 'motorista'}`}>
        <Text style={s.perfilNome} numberOfLines={1}>{primeiro}</Text>
        <View style={s.avatar}><Text style={s.avatarTexto}>{iniciais}</Text></View>
      </Pressable>
    );
  };

  return (
    <Tabs
      screenOptions={{
        headerStyle: { backgroundColor: cores.primaria }, headerTintColor: '#fff', headerTitleStyle: { fontWeight: '700' },
        headerRight: perfilNoTopo,
        tabBarActiveTintColor: cores.acento, tabBarInactiveTintColor: cores.textoSuave,
        tabBarLabelStyle: { fontSize: 12, fontWeight: '600' }, tabBarStyle: { height: 64, paddingBottom: 8, paddingTop: 6 },
      }}
    >
      <Tabs.Screen name="rotas" options={{ title: 'Rotas', tabBarIcon: icone('map') }} />
      <Tabs.Screen name="ofertas" options={{ title: 'Ofertas', tabBarIcon: icone('hand-right') }} />
      <Tabs.Screen name="financeiro" options={{ title: 'Financeiro', tabBarIcon: icone('cash') }} />
      <Tabs.Screen name="disponibilidade" options={{ title: 'Agenda', tabBarIcon: icone('calendar') }} />
      {/* Ajuda (Hugo, 12/09): chat com o assistente e com a logística.
          O badge mostra o que a equipe respondeu e o motorista ainda não viu. */}
      <Tabs.Screen name="ajuda" options={{ title: 'Ajuda', tabBarIcon: icone('chatbubble-ellipses'), tabBarBadge: naoLidas || undefined }} />
      <Tabs.Screen name="perfil" options={{ title: 'Perfil', href: null, headerRight: undefined }} />
    </Tabs>
  );
}

const s = StyleSheet.create({
  perfil: { flexDirection: 'row', alignItems: 'center', gap: 8, marginRight: 14, maxWidth: 180 },
  perfilNome: { color: '#fff', fontWeight: '700', fontSize: 14, flexShrink: 1 },
  avatar: { width: 32, height: 32, borderRadius: 16, backgroundColor: '#fff', alignItems: 'center', justifyContent: 'center' },
  avatarTexto: { color: cores.primaria, fontWeight: '800', fontSize: 13 },
});
