import React, { useCallback, useState } from 'react';
import { Pressable, RefreshControl, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useFocusEffect } from 'expo-router';
import * as api from '../../src/api';
import { Cartao, Linha, Vazio } from '../../src/componentes';
import { cores, formatarData, formatarReal, hoje, somarDias } from '../../src/tema';
import type { Extrato } from '../../src/tipos';

type Periodo = 'semana' | 'mes' | 'anterior';

function intervalo(p: Periodo): { de: string; ate: string; rotulo: string } {
  const h = hoje();
  if (p === 'semana') return { de: somarDias(h, -6), ate: h, rotulo: 'Últimos 7 dias' };
  if (p === 'mes') return { de: `${h.slice(0, 7)}-01`, ate: h, rotulo: 'Este mês' };
  const d = new Date(`${h.slice(0, 7)}-01T12:00:00`);
  d.setDate(0);
  const fim = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  return { de: `${fim.slice(0, 7)}-01`, ate: fim, rotulo: 'Mês anterior' };
}

export default function Financeiro() {
  const [periodo, setPeriodo] = useState<Periodo>('semana');
  const [extrato, setExtrato] = useState<Extrato | null>(null);
  const [carregando, setCarregando] = useState(true);
  const [erro, setErro] = useState<string | null>(null);

  const carregar = useCallback(async () => {
    setErro(null);
    const { de, ate } = intervalo(periodo);
    try {
      setExtrato(await api.financeiro(de, ate));
    } catch (e) {
      setErro(e instanceof api.ErroRede ? 'Sem conexão.' : (e as Error).message);
    } finally {
      setCarregando(false);
    }
  }, [periodo]);
  useFocusEffect(useCallback(() => { void carregar(); }, [carregar]));

  return (
    <ScrollView style={s.tela} contentContainerStyle={{ padding: 16, paddingBottom: 40 }}
      refreshControl={<RefreshControl refreshing={carregando} onRefresh={carregar} />}>
      <View style={s.abas}>
        {(['semana', 'mes', 'anterior'] as Periodo[]).map((p) => (
          <Pressable key={p} onPress={() => setPeriodo(p)} style={[s.aba, periodo === p && s.abaAtiva]}>
            <Text style={[s.abaTexto, periodo === p && { color: '#fff' }]}>{intervalo(p).rotulo}</Text>
          </Pressable>
        ))}
      </View>
      {erro ? <Text style={s.erro}>{erro}</Text> : null}
      {extrato ? (
        <>
          <Cartao estilo={{ backgroundColor: cores.primaria, borderColor: cores.primaria }}>
            <Text style={s.totalRotulo}>{intervalo(periodo).rotulo}</Text>
            <Text style={s.total}>{formatarReal(extrato.total)}</Text>
            <Text style={s.totalSub}>{extrato.linhas.length} rota(s)</Text>
            {extrato.valores_provisorios ? <Text style={s.totalSub}>⚠ Parte dos km é estimada — o valor final sai com o km real.</Text> : null}
            {extrato.rotas_sem_tarifa > 0 ? <Text style={s.totalSub}>⚠ {extrato.rotas_sem_tarifa} rota(s) com tarifa a definir.</Text> : null}
          </Cartao>
          {extrato.tarifa ? (
            <Cartao>
              <Text style={s.secao}>Sua tarifa · {extrato.tarifa.nome_tarifa}</Text>
              <Linha rotulo="Valor da saída" valor={formatarReal(extrato.tarifa.valor_base)} />
              <Linha rotulo="Km inclusos" valor={`${extrato.tarifa.km_franquia} km`} />
              <Linha rotulo="Km adicional" valor={`${formatarReal(extrato.tarifa.valor_km_adicional)} / km`} />
            </Cartao>
          ) : null}
          {extrato.por_dia.length === 0 ? <Vazio texto="Nenhuma rota no período." /> : null}
          {[...extrato.por_dia].reverse().map((d) => (
            <Cartao key={d.data}>
              <View style={s.diaCab}>
                <Text style={s.dia}>{formatarData(d.data)}</Text>
                <Text style={s.diaValor}>{formatarReal(d.valor)}</Text>
              </View>
              {extrato.linhas.filter((l) => l.data_rota === d.data).map((l) => (
                <View key={l.rota_id} style={s.rota}>
                  <Text style={s.rotaNome} numberOfLines={1}>{l.nome ?? `Rota #${l.rota_id}`}</Text>
                  <Text style={s.rotaDetalhe}>
                    {l.entregues}/{l.total_paradas} entregues · {l.km !== null ? `${l.km.toFixed(1)} km${l.km_provisorio ? ' (est.)' : ''}` : 'km —'} · {l.sem_tarifa ? 'a definir' : formatarReal(l.valor)}
                  </Text>
                </View>
              ))}
            </Cartao>
          ))}
        </>
      ) : null}
    </ScrollView>
  );
}

const s = StyleSheet.create({
  tela: { flex: 1, backgroundColor: cores.fundo },
  abas: { flexDirection: 'row', gap: 8, marginBottom: 12 },
  aba: { flex: 1, padding: 10, borderRadius: 10, backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda, alignItems: 'center' },
  abaAtiva: { backgroundColor: cores.primaria, borderColor: cores.primaria },
  // (aba ativa em navy, valor em verde do logo sobre o cartão navy)
  abaTexto: { fontWeight: '700', color: cores.texto, fontSize: 12 },
  totalRotulo: { color: cores.textoSuaveSobrePrimaria, fontWeight: '600' },
  total: { color: cores.acentoLogo, fontSize: 34, fontWeight: '900', marginVertical: 4 },
  totalSub: { color: cores.textoSuaveSobrePrimaria, marginTop: 2 },
  secao: { fontWeight: '800', color: cores.textoSuave, marginBottom: 6 },
  diaCab: { flexDirection: 'row', justifyContent: 'space-between', marginBottom: 6 },
  dia: { fontWeight: '800', color: cores.texto, fontSize: 16 },
  diaValor: { fontWeight: '800', color: cores.primaria, fontSize: 16 },
  rota: { paddingVertical: 6, borderTopWidth: StyleSheet.hairlineWidth, borderColor: cores.borda },
  rotaNome: { color: cores.texto, fontWeight: '600' },
  rotaDetalhe: { color: cores.textoSuave, fontSize: 13 },
  erro: { color: '#fff', backgroundColor: cores.alerta, padding: 8, borderRadius: 8, marginBottom: 12, textAlign: 'center' },
});
