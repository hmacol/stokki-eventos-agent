// Cartao de baixo do Mapa da rota: uma parada por vez. Troca por setas
// (nunca arrastando o cartao pro lado: o arrastar e do Deslizar, regra do
// Hugo de 26/08) e pelo toque no pino. Os passos sao os mesmos da lista da
// tela da rota (app/rota/[id].tsx): iniciar deslocamento -> cheguei -> e so
// depois Entregue / Nao entregue, que abrem a tela da parada com o resultado
// ja escolhido (checklist, fotos e canhoto continuam la).
import React from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { Deslizar } from '../deslizar';
import { cores, situacaoCor, situacaoRotulo } from '../tema';
import { nomeExibicao, partesEndereco, resumoJanela } from '../textos';
import { abrirNavegacao, ehPendente, emAndamento, rotaOperavel } from '../paradaPasso';
import type { Parada, Rota } from '../tipos';

export type FluxoBaixa = 'ENTREGUE' | 'NAO_ENTREGUE';

/** Parada sem coordenada (nula ou 0,0) nao ganha pino. */
export function temCoordenada(p: Parada): boolean {
  return p.latitude !== null && p.longitude !== null && !(p.latitude === 0 && p.longitude === 0);
}

export function CartaoParada({ parada: p, rota, total, indice, onAnterior, onProxima, onPasso, onBaixa }: {
  parada: Parada;
  rota: Rota;
  total: number;
  /** posicao do cartao, comecando em 0 */
  indice: number;
  onAnterior: () => void;
  onProxima: () => void;
  onPasso: (p: Parada, tipo: 'DESLOCAMENTO' | 'CHEGADA') => Promise<void>;
  onBaixa: (p: Parada, fluxo: FluxoBaixa) => void;
}) {
  const end = partesEndereco(p.endereco);
  const janela = resumoJanela(p, rota.data_rota, Date.now());
  const pendente = ehPendente(p);
  const temAnterior = indice > 0;
  const temProxima = indice < total - 1;

  /** Mesmo controle da lista, so que no fim dois Deslizar em vez de "Finalizar entrega". */
  const controle = () => {
    if (!rotaOperavel(rota) || !pendente) return null;
    const ativa = rota.paradas.find(emAndamento) ?? null;
    if (ativa && ativa.id !== p.id) {
      return <Text style={s.travada}>Finalize a parada {ativa.ordem} pra liberar esta.</Text>;
    }
    if (p.situacao === 'PENDENTE') {
      return (
        <Deslizar key={`${p.id}-desloc`} titulo={p.tentativas > 0 ? 'Voltar: iniciar deslocamento' : 'Iniciar deslocamento'}
          icone="navigate" onConfirmar={() => onPasso(p, 'DESLOCAMENTO')} />
      );
    }
    if (p.situacao === 'EM_DESLOCAMENTO') {
      return <Deslizar key={`${p.id}-chegada`} titulo="Cheguei no local" icone="location" cor={cores.info} onConfirmar={() => onPasso(p, 'CHEGADA')} />;
    }
    return (
      <View style={{ gap: 10 }}>
        <Deslizar key={`${p.id}-entregue`} titulo="Entregue" icone="checkmark-done" cor={cores.acento} onConfirmar={() => onBaixa(p, 'ENTREGUE')} />
        <Deslizar key={`${p.id}-nao`} titulo="Não entregue" icone="close" cor={cores.perigo} onConfirmar={() => onBaixa(p, 'NAO_ENTREGUE')} />
      </View>
    );
  };

  const acao = controle();
  const leitura = rota.editavel && (rota.status === 'PLANEJADA' || rota.status === 'ACEITA')
    ? 'Aceite e inicie a rota na tela da rota pra dar baixa por aqui.'
    : null;

  return (
    <View style={s.cartao}>
      <View style={s.cabeca}>
        <Pressable style={[s.seta, !temAnterior && s.setaInativa]} onPress={onAnterior} disabled={!temAnterior}
          accessibilityRole="button" accessibilityLabel="Parada anterior" hitSlop={6}>
          <Ionicons name="chevron-back" size={26} color={cores.primaria} />
        </Pressable>
        <View style={[s.ordem, !pendente && { backgroundColor: situacaoCor[p.situacao] }]}>
          <Text style={s.ordemTexto}>{p.ordem}</Text>
        </View>
        <View style={{ flex: 1 }}>
          <Text style={s.nome} numberOfLines={1}>{nomeExibicao(p)}</Text>
          <Text style={[s.situacao, { color: situacaoCor[p.situacao] }]}>
            {situacaoRotulo[p.situacao]}
            {p.reagendado_para && pendente ? ' · volta depois' : ''}
          </Text>
        </View>
        <Pressable style={[s.seta, !temProxima && s.setaInativa]} onPress={onProxima} disabled={!temProxima}
          accessibilityRole="button" accessibilityLabel="Próxima parada" hitSlop={6}>
          <Ionicons name="chevron-forward" size={26} color={cores.primaria} />
        </Pressable>
      </View>

      <View style={s.corpo}>
        <View style={{ flex: 1 }}>
          <Text style={s.endereco} numberOfLines={2}>{end.completo ? `${end.rua}${end.bairro ? ` — ${end.bairro}` : ''}` : 'Endereço não informado'}</Text>
          <Text style={s.linha} numberOfLines={2}>
            {[p.codigo, p.remetente_nome ? nomeExibicao({ destinatario_nome: p.remetente_nome }) : null, janela.texto].filter(Boolean).join(' · ')}
          </Text>
        </View>
        {pendente ? (
          <Pressable style={s.navegar} onPress={() => abrirNavegacao(p)} accessibilityRole="button" accessibilityLabel="Navegar até a parada">
            <Ionicons name="navigate" size={17} color={cores.primaria} />
            <Text style={s.navegarTexto}>Navegar</Text>
          </Pressable>
        ) : null}
      </View>
      {!temCoordenada(p) ? (
        <View style={s.semLocal}>
          <Ionicons name="warning" size={15} color={cores.alerta} />
          <Text style={s.semLocalTexto}>Sem localização no mapa</Text>
        </View>
      ) : null}
      {leitura ? <Text style={s.leitura}>{leitura}</Text> : null}

      {acao ? <View style={{ marginTop: 12 }}>{acao}</View> : null}
    </View>
  );
}

const s = StyleSheet.create({
  cartao: { backgroundColor: cores.cartao, borderTopLeftRadius: 18, borderTopRightRadius: 18, paddingHorizontal: 16, paddingTop: 8, paddingBottom: 14, elevation: 8 },
  cabeca: { flexDirection: 'row', alignItems: 'center', gap: 8, marginHorizontal: -8 },
  seta: { width: 44, height: 48, alignItems: 'center', justifyContent: 'center', borderRadius: 12 },
  setaInativa: { opacity: 0.25 },
  corpo: { flexDirection: 'row', alignItems: 'center', gap: 10, marginTop: 8 },
  ordem: { width: 34, height: 34, borderRadius: 17, backgroundColor: cores.primaria, alignItems: 'center', justifyContent: 'center' },
  ordemTexto: { color: '#fff', fontWeight: '800', fontSize: 15 },
  nome: { fontSize: 18, fontWeight: '800', color: cores.texto },
  situacao: { fontSize: 12, fontWeight: '700', textTransform: 'uppercase', letterSpacing: 0.5, marginTop: 1 },
  navegar: { flexDirection: 'row', alignItems: 'center', gap: 5, height: 44, paddingHorizontal: 12, borderRadius: 12, backgroundColor: cores.fundo },
  navegarTexto: { fontSize: 13, fontWeight: '700', color: cores.texto },
  endereco: { color: cores.texto, fontSize: 14 },
  linha: { color: cores.textoSuave, fontSize: 13, marginTop: 3 },
  semLocal: { flexDirection: 'row', alignItems: 'center', gap: 6, marginTop: 8, backgroundColor: cores.alertaBg, borderRadius: 8, paddingHorizontal: 10, paddingVertical: 6 },
  semLocalTexto: { color: '#7A5211', fontWeight: '700', fontSize: 13 },
  leitura: { color: cores.textoSuave, fontStyle: 'italic', fontSize: 13, marginTop: 8 },
  travada: { color: cores.textoSuave, fontStyle: 'italic', textAlign: 'center', paddingVertical: 6 },
});
