import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useSessao } from '../../src/sessao';
import { Botao, Cartao, Linha } from '../../src/componentes';
import * as fila from '../../src/fila';
import { API_URL } from '../../src/api';
import { cores } from '../../src/tema';

const ROTULO_TIPO: Record<string, string> = {
  EVENTO_PARADA: 'Parada', COMPROVANTE: 'Foto', PEDAGIO: 'Pedágio', GPS: 'GPS', ROTA: 'Rota',
};

export default function Perfil() {
  const { motorista, sair } = useSessao();
  const [itens, setItens] = useState<Awaited<ReturnType<typeof fila.listar>>>([]);
  const [descartados, setDescartados] = useState<{ motivo: string; em: string }[]>([]);
  const [ultimoErro, setUltimoErro] = useState<fila.UltimoErro | null>(null);
  const pendentes = itens.length;

  const atualizar = useCallback(async () => {
    setItens(await fila.listar());
    setDescartados((await fila.descartados()).map((x) => ({ motivo: x.motivo, em: x.em })));
    setUltimoErro(await fila.ultimoErro());
  }, []);

  useEffect(() => {
    void atualizar();
    return fila.aoMudar(() => void atualizar());
  }, [atualizar]);

  const tentarAgora = async () => {
    const r = await fila.processar();
    await atualizar();
    Alert.alert('Fila', `${r.enviados} enviado(s), ${r.pendentes} pendente(s).`);
  };

  const descartar = (uuid: string, detalhe: string) =>
    Alert.alert('Descartar registro?', `${detalhe}\n\nEle NÃO será enviado ao sistema.`, [
      { text: 'Voltar', style: 'cancel' },
      { text: 'Descartar', style: 'destructive', onPress: () => void fila.descartarItem(uuid).then(atualizar) },
    ]);

  const confirmarSaida = () =>
    Alert.alert('Sair do app?', pendentes > 0 ? `Ainda há ${pendentes} registro(s) sem enviar. Saia só quando estiver com sinal.` : undefined, [
      { text: 'Voltar', style: 'cancel' },
      { text: 'Sair', style: 'destructive', onPress: () => void sair() },
    ]);

  return (
    <ScrollView style={s.tela} contentContainerStyle={{ padding: 16, paddingBottom: 40 }}>
      <Cartao>
        <Text style={s.nome}>{motorista?.nome}</Text>
        <Linha rotulo="CPF" valor={motorista?.cpf} />
        <Linha rotulo="Veículo" valor={motorista?.tipo_veiculo ?? 'Fiorino (padrão)'} />
        <Linha rotulo="Telefone" valor={motorista?.telefone ?? '—'} />
        {motorista?.perfil === 'TESTE' ? <Text style={s.teste}>Usuário de teste</Text> : null}
      </Cartao>
      <Cartao>
        <Linha rotulo="Registros aguardando envio" valor={String(pendentes)} />
        <Botao titulo="Tentar enviar agora" tipo="secundario" onPress={() => void tentarAgora()} estilo={{ marginTop: 10 }} />
        {ultimoErro ? (
          <Text style={s.erro}>Último erro ({ultimoErro.em.slice(5, 16)}, {ROTULO_TIPO[ultimoErro.tipo] ?? ultimoErro.tipo}): {ultimoErro.mensagem}</Text>
        ) : null}
        {itens.length > 0 ? (
          <>
            <Text style={s.descTitulo}>Na fila</Text>
            {itens.map((i) => (
              <View key={i.uuid} style={s.item}>
                <View style={{ flex: 1 }}>
                  <Text style={s.itemTitulo}>{ROTULO_TIPO[i.tipo] ?? i.tipo} · {i.detalhe}</Text>
                  <Text style={s.desc}>{i.criadoEm.slice(5, 16)}{i.tentativas > 0 ? ` · ${i.tentativas} tentativa(s)` : ''}</Text>
                </View>
                <Pressable onPress={() => descartar(i.uuid, `${ROTULO_TIPO[i.tipo] ?? i.tipo} · ${i.detalhe}`)} hitSlop={8}>
                  <Text style={s.descartar}>Descartar</Text>
                </Pressable>
              </View>
            ))}
          </>
        ) : null}
        {descartados.length > 0 ? (
          <>
            <Text style={s.descTitulo}>Registros recusados pelo sistema</Text>
            {descartados.slice(0, 5).map((d, i) => (
              <Text key={i} style={s.desc}>{d.em.slice(0, 16)} — {d.motivo}</Text>
            ))}
          </>
        ) : null}
      </Cartao>
      <Botao titulo="Sair" tipo="perigo" onPress={confirmarSaida} />
      <Text style={s.rodape}>FreshLog Motorista · {API_URL}</Text>
    </ScrollView>
  );
}

const s = StyleSheet.create({
  tela: { flex: 1, backgroundColor: cores.fundo },
  nome: { fontSize: 20, fontWeight: '800', color: cores.texto, marginBottom: 8 },
  teste: { color: cores.alerta, fontWeight: '700', marginTop: 8 },
  erro: { color: cores.perigo, fontSize: 12, marginTop: 10 },
  descTitulo: { fontWeight: '700', color: cores.perigo, marginTop: 12 },
  item: { flexDirection: 'row', alignItems: 'center', gap: 10, paddingVertical: 8, borderTopWidth: StyleSheet.hairlineWidth, borderColor: cores.borda, marginTop: 6 },
  itemTitulo: { color: cores.texto, fontWeight: '600', fontSize: 13 },
  descartar: { color: cores.perigo, fontWeight: '700', fontSize: 13 },
  desc: { color: cores.textoSuave, fontSize: 12, marginTop: 4 },
  rodape: { color: cores.textoSuave, fontSize: 11, textAlign: 'center', marginTop: 24 },
});
