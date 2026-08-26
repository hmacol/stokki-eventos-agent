import React, { useCallback, useState } from 'react';
import { Alert, Pressable, RefreshControl, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import { useFocusEffect } from 'expo-router';
import * as api from '../../src/api';
import { Botao, Cartao } from '../../src/componentes';
import { cores, formatarData, hoje, somarDias } from '../../src/tema';
import type { Ajuste } from '../../src/tipos';

const DIAS = ['Dom', 'Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb'];

export default function Disponibilidade() {
  const [ajustes, setAjustes] = useState<Record<string, Ajuste>>({});
  const [carregando, setCarregando] = useState(true);
  const [selecionados, setSelecionados] = useState<string[]>([]);
  const [motivo, setMotivo] = useState('');
  const h = hoje();
  const de = h;
  const ate = somarDias(h, 27);

  const carregar = useCallback(async () => {
    try {
      const lista = await api.disponibilidade(de, ate);
      setAjustes(Object.fromEntries(lista.map((a) => [a.data, a])));
    } catch (e) {
      if (!(e instanceof api.ErroRede)) Alert.alert('Erro', (e as Error).message);
    } finally {
      setCarregando(false);
    }
  }, [de, ate]);
  useFocusEffect(useCallback(() => { void carregar(); }, [carregar]));

  const dias = Array.from({ length: 28 }, (_, i) => somarDias(h, i));
  const alternar = (d: string) => setSelecionados((s) => (s.includes(d) ? s.filter((x) => x !== d) : [...s, d]));

  const aplicar = async (disponivel: boolean | null) => {
    if (selecionados.length === 0) return;
    try {
      for (const d of selecionados) await api.definirDisponibilidade({ de: d, ate: d, disponivel, motivo: motivo || null });
      setSelecionados([]);
      setMotivo('');
      await carregar();
    } catch (e) {
      Alert.alert('Não deu', e instanceof api.ErroRede ? 'Sem conexão.' : (e as Error).message);
    }
  };

  return (
    <ScrollView style={s.tela} contentContainerStyle={{ padding: 16, paddingBottom: 40 }}
      refreshControl={<RefreshControl refreshing={carregando} onRefresh={carregar} />}>
      <Text style={s.ajuda}>Toque nos dias e marque se você está disponível ou não. Sem marcação, vale sua escala normal.</Text>
      <View style={s.grade}>
        {dias.map((d) => {
          const a = ajustes[d];
          const sel = selecionados.includes(d);
          const cor = a ? (a.disponivel ? cores.primaria : cores.perigo) : '#fff';
          const dt = new Date(`${d}T12:00:00`);
          return (
            <Pressable key={d} onPress={() => alternar(d)} style={[s.dia, { backgroundColor: cor, borderColor: sel ? cores.texto : cores.borda, borderWidth: sel ? 3 : 1 }]}>
              <Text style={[s.diaSemana, a && { color: '#fff' }]}>{DIAS[dt.getDay()]}</Text>
              <Text style={[s.diaNum, a && { color: '#fff' }]}>{dt.getDate()}</Text>
            </Pressable>
          );
        })}
      </View>
      <View style={s.legenda}>
        <Text style={[s.legItem, { color: cores.primaria }]}>■ Disponível</Text>
        <Text style={[s.legItem, { color: cores.perigo }]}>■ Indisponível</Text>
        <Text style={s.legItem}>□ Escala normal</Text>
      </View>
      {selecionados.length > 0 ? (
        <Cartao>
          <Text style={s.selTitulo}>{selecionados.length} dia(s) selecionado(s): {selecionados.sort().map(formatarData).join(', ')}</Text>
          <TextInput style={s.campo} placeholder="Motivo (opcional): férias, consulta…" value={motivo} onChangeText={setMotivo} />
          <View style={{ gap: 8, marginTop: 10 }}>
            <Botao titulo="Estou disponível" onPress={() => aplicar(true)} />
            <Botao titulo="Não estou disponível" tipo="perigo" onPress={() => aplicar(false)} />
            <Botao titulo="Voltar à escala normal" tipo="secundario" onPress={() => aplicar(null)} />
          </View>
        </Cartao>
      ) : null}
    </ScrollView>
  );
}

const s = StyleSheet.create({
  tela: { flex: 1, backgroundColor: cores.fundo },
  ajuda: { color: cores.textoSuave, marginBottom: 12 },
  grade: { flexDirection: 'row', flexWrap: 'wrap', gap: 6 },
  dia: { width: '12.5%', flexGrow: 1, aspectRatio: 1, borderRadius: 10, alignItems: 'center', justifyContent: 'center' },
  diaSemana: { fontSize: 11, color: cores.textoSuave },
  diaNum: { fontSize: 16, fontWeight: '800', color: cores.texto },
  legenda: { flexDirection: 'row', gap: 16, marginVertical: 12 },
  legItem: { color: cores.textoSuave, fontSize: 12, fontWeight: '600' },
  selTitulo: { fontWeight: '700', color: cores.texto },
  campo: { backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda, borderRadius: 10, padding: 12, marginTop: 10 },
});
