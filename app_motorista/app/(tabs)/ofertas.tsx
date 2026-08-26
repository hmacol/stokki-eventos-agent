import React, { useCallback, useState } from 'react';
import { Alert, RefreshControl, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useFocusEffect } from 'expo-router';
import * as api from '../../src/api';
import { Botao, Cartao, Vazio } from '../../src/componentes';
import { cores, formatarData } from '../../src/tema';
import type { Oferta } from '../../src/tipos';

function Resumo({ resumo }: { resumo: Record<string, unknown> }) {
  // O resumo vem de regras/resumo_oferta.py (região, paradas por nível,
  // caixas, peso quando é dado real) -- mostra chave/valor sem depender do formato exato.
  const linhas = Object.entries(resumo).filter(([, v]) => v !== null && v !== undefined && v !== '' && typeof v !== 'object');
  const listas = Object.entries(resumo).filter(([, v]) => Array.isArray(v)) as [string, unknown[]][];
  return (
    <View style={{ marginTop: 8 }}>
      {linhas.map(([k, v]) => (
        <Text key={k} style={s.linha}><Text style={s.chave}>{k.replace(/_/g, ' ')}: </Text>{String(v)}</Text>
      ))}
      {listas.map(([k, v]) => (
        <Text key={k} style={s.linha}><Text style={s.chave}>{k.replace(/_/g, ' ')}: </Text>{v.map(String).join(', ')}</Text>
      ))}
    </View>
  );
}

export default function Ofertas() {
  const [dados, setDados] = useState<{ abertas: Oferta[]; minhas: Oferta[] }>({ abertas: [], minhas: [] });
  const [carregando, setCarregando] = useState(true);
  const [erro, setErro] = useState<string | null>(null);

  const carregar = useCallback(async () => {
    setErro(null);
    try {
      setDados(await api.ofertas());
    } catch (e) {
      setErro(e instanceof api.ErroRede ? 'Sem conexão.' : (e as Error).message);
    } finally {
      setCarregando(false);
    }
  }, []);
  useFocusEffect(useCallback(() => { void carregar(); }, [carregar]));

  const escolher = (o: Oferta) =>
    Alert.alert('Escolher esta rota?', 'A escolha vale por ordem de chegada. Depois a operação confirma e envia a rota.', [
      { text: 'Voltar', style: 'cancel' },
      { text: 'Escolher', onPress: async () => {
        try { setDados(await api.escolherOferta(o.rascunho_id)); } catch (e) { Alert.alert('Não deu', (e as Error).message); void carregar(); }
      } },
    ]);

  const cancelar = (o: Oferta) =>
    Alert.alert('Desistir desta rota?', undefined, [
      { text: 'Voltar', style: 'cancel' },
      { text: 'Desistir', style: 'destructive', onPress: async () => {
        try { setDados(await api.cancelarOferta(o.rascunho_id)); } catch (e) { Alert.alert('Não deu', (e as Error).message); void carregar(); }
      } },
    ]);

  return (
    <ScrollView style={s.tela} contentContainerStyle={{ padding: 16, paddingBottom: 40 }}
      refreshControl={<RefreshControl refreshing={carregando} onRefresh={carregar} />}>
      {erro ? <Text style={s.erro}>{erro}</Text> : null}
      {dados.minhas.length > 0 ? <Text style={s.secao}>Escolhidas por você</Text> : null}
      {dados.minhas.map((o) => (
        <Cartao key={o.rascunho_id} estilo={{ borderColor: cores.primaria }}>
          <Text style={s.titulo}>Rota de {formatarData(o.data_alvo)}</Text>
          <Resumo resumo={o.resumo} />
          {o.aplicada ? <Text style={s.info}>Já confirmada pela operação.</Text> : <Botao titulo="Desistir" tipo="secundario" onPress={() => cancelar(o)} estilo={{ marginTop: 12 }} />}
        </Cartao>
      ))}
      <Text style={s.secao}>Disponíveis</Text>
      {dados.abertas.length === 0 && !carregando ? <Vazio texto="Nenhuma rota em oferta agora. Quando a operação publicar, você recebe um aviso." /> : null}
      {dados.abertas.map((o) => (
        <Cartao key={o.rascunho_id}>
          <Text style={s.titulo}>Rota de {formatarData(o.data_alvo)}</Text>
          <Resumo resumo={o.resumo} />
          <Botao titulo="Quero esta rota" onPress={() => escolher(o)} estilo={{ marginTop: 12 }} />
        </Cartao>
      ))}
    </ScrollView>
  );
}

const s = StyleSheet.create({
  tela: { flex: 1, backgroundColor: cores.fundo },
  secao: { fontSize: 15, fontWeight: '800', color: cores.textoSuave, marginVertical: 8, textTransform: 'uppercase' },
  titulo: { fontSize: 17, fontWeight: '700', color: cores.texto },
  linha: { color: cores.texto, marginTop: 2 },
  chave: { color: cores.textoSuave, textTransform: 'capitalize' },
  info: { color: cores.primaria, marginTop: 10, fontWeight: '600' },
  erro: { color: '#fff', backgroundColor: cores.alerta, padding: 8, borderRadius: 8, marginBottom: 12, textAlign: 'center' },
});
