import React, { useCallback, useEffect, useState } from 'react';
import { FlatList, Pressable, RefreshControl, StyleSheet, Text, View } from 'react-native';
import { useFocusEffect, useRouter } from 'expo-router';
import { Cartao, Etiqueta, Vazio } from '../../src/componentes';
import { carregarRotas } from '../../src/rotasStore';
import * as fila from '../../src/fila';
import { cores, formatarData, hoje, somarDias, statusRotaCor, statusRotaRotulo } from '../../src/tema';
import type { Rota } from '../../src/tipos';

const corStatus = statusRotaCor;

export default function Rotas() {
  const router = useRouter();
  const [rotas, setRotas] = useState<Rota[]>([]);
  const [offline, setOffline] = useState(false);
  const [carregando, setCarregando] = useState(true);
  const [erro, setErro] = useState<string | null>(null);
  const [pendentes, setPendentes] = useState(0);

  const carregar = useCallback(async () => {
    setErro(null);
    try {
      const r = await carregarRotas();
      setRotas(r.rotas);
      setOffline(r.offline);
    } catch (e) {
      setErro((e as Error).message);
    } finally {
      setCarregando(false);
    }
  }, []);

  useFocusEffect(useCallback(() => { void carregar(); }, [carregar]));
  useEffect(() => {
    void fila.tamanho().then(setPendentes);
    return fila.aoMudar(setPendentes);
  }, []);

  const h = hoje();
  const grupo = (data: string) => rotas.filter((r) => r.data_rota === data);
  const secoes: { titulo: string; itens: Rota[] }[] = [
    { titulo: 'Hoje', itens: grupo(h) },
    { titulo: `Amanhã · ${formatarData(somarDias(h, 1))}`, itens: grupo(somarDias(h, 1)) },
    { titulo: `Ontem · ${formatarData(somarDias(h, -1))}`, itens: grupo(somarDias(h, -1)) },
  ];

  return (
    <View style={s.tela}>
      {offline ? <Text style={s.aviso}>Sem conexão — mostrando a última versão salva.</Text> : null}
      {pendentes > 0 ? <Text style={s.fila}>{pendentes} registro(s) aguardando envio.</Text> : null}
      {erro ? <Text style={[s.aviso, { backgroundColor: cores.perigo }]}>{erro}</Text> : null}
      <FlatList
        contentContainerStyle={{ padding: 16, paddingBottom: 40 }}
        data={secoes}
        keyExtractor={(sec) => sec.titulo}
        refreshControl={<RefreshControl refreshing={carregando} onRefresh={carregar} />}
        ListEmptyComponent={<Vazio texto="Nenhuma rota." />}
        renderItem={({ item: sec }) => (
          <View style={{ marginBottom: 8 }}>
            <Text style={s.secao}>{sec.titulo}</Text>
            {sec.itens.length === 0 ? <Text style={s.nada}>Nenhuma rota.</Text> : null}
            {sec.itens.map((r) => (
              <Pressable key={r.id} onPress={() => router.push(`/rota/${r.id}`)}>
                <Cartao>
                  <View style={s.cabecalho}>
                    <Text style={s.nome} numberOfLines={1}>{r.nome ?? `Rota #${r.id}`}</Text>
                    <Etiqueta texto={statusRotaRotulo[r.status] ?? r.status} cor={corStatus[r.status] ?? '#777'} />
                  </View>
                  <Text style={s.detalhe}>
                    {r.total_paradas} parada(s) · {r.entregues} entregue(s) · {r.insucessos} insucesso(s)
                    {r.start_at ? ` · saída ${r.start_at.slice(11, 16)}` : ''}
                  </Text>
                  {r.provedor === 'VUUPT' ? <Text style={s.vuupt}>Operada pelo app da VUUPT — aqui só consulta e aceite.</Text> : null}
                  {r.status === 'PLANEJADA' ? <Text style={s.chamada}>Toque para aceitar ou recusar →</Text> : null}
                </Cartao>
              </Pressable>
            ))}
          </View>
        )}
      />
    </View>
  );
}

const s = StyleSheet.create({
  tela: { flex: 1, backgroundColor: cores.fundo },
  aviso: { backgroundColor: cores.alerta, color: '#fff', padding: 8, textAlign: 'center', fontWeight: '600' },
  fila: { backgroundColor: cores.info, color: '#fff', padding: 6, textAlign: 'center' },
  secao: { fontSize: 15, fontWeight: '800', color: cores.textoSuave, marginBottom: 8, marginTop: 8, textTransform: 'uppercase' },
  nada: { color: cores.textoSuave, marginBottom: 12 },
  cabecalho: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: 8 },
  nome: { fontSize: 17, fontWeight: '700', color: cores.texto, flexShrink: 1 },
  detalhe: { color: cores.textoSuave, marginTop: 6 },
  vuupt: { color: cores.info, marginTop: 6, fontSize: 12 },
  chamada: { color: cores.acento, marginTop: 8, fontWeight: '700' },
});
