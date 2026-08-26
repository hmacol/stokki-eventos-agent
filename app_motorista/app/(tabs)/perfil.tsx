import React, { useEffect, useState } from 'react';
import { Alert, ScrollView, StyleSheet, Text } from 'react-native';
import { useSessao } from '../../src/sessao';
import { Botao, Cartao, Linha } from '../../src/componentes';
import * as fila from '../../src/fila';
import { API_URL } from '../../src/api';
import { cores } from '../../src/tema';

export default function Perfil() {
  const { motorista, sair } = useSessao();
  const [pendentes, setPendentes] = useState(0);
  const [descartados, setDescartados] = useState<{ motivo: string; em: string }[]>([]);

  useEffect(() => {
    void fila.tamanho().then(setPendentes);
    void fila.descartados().then((d) => setDescartados(d.map((x) => ({ motivo: x.motivo, em: x.em }))));
    return fila.aoMudar(setPendentes);
  }, []);

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
        <Botao titulo="Tentar enviar agora" tipo="secundario" onPress={() => void fila.processar().then((r) => Alert.alert('Fila', `${r.enviados} enviado(s), ${r.pendentes} pendente(s).`))} estilo={{ marginTop: 10 }} />
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
  descTitulo: { fontWeight: '700', color: cores.perigo, marginTop: 12 },
  desc: { color: cores.textoSuave, fontSize: 12, marginTop: 4 },
  rodape: { color: cores.textoSuave, fontSize: 11, textAlign: 'center', marginTop: 24 },
});
