import React from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View, ViewStyle } from 'react-native';
import { cores } from './tema';

export function Botao({
  titulo, onPress, tipo = 'primario', desabilitado, carregando, estilo,
}: {
  titulo: string; onPress: () => void; tipo?: 'primario' | 'secundario' | 'perigo' | 'alerta';
  desabilitado?: boolean; carregando?: boolean; estilo?: ViewStyle;
}) {
  // Ação principal em verde (acento do logo), como o botão "Confirmar" da
  // página de confirmação; navy fica pra cabeçalhos e seleção.
  const fundo = { primario: cores.acento, secundario: cores.cartao, perigo: cores.perigo, alerta: cores.alerta }[tipo];
  const textoCor = tipo === 'secundario' ? cores.texto : '#fff';
  return (
    <Pressable
      onPress={onPress}
      disabled={desabilitado || carregando}
      style={({ pressed }) => [
        s.botao,
        { backgroundColor: fundo, opacity: desabilitado ? 0.5 : pressed ? 0.85 : 1, borderWidth: tipo === 'secundario' ? 1 : 0 },
        estilo,
      ]}
    >
      {carregando ? <ActivityIndicator color={textoCor} /> : <Text style={[s.botaoTexto, { color: textoCor }]}>{titulo}</Text>}
    </Pressable>
  );
}

export function Cartao({ children, estilo }: { children: React.ReactNode; estilo?: ViewStyle }) {
  return <View style={[s.cartao, estilo]}>{children}</View>;
}

export function Etiqueta({ texto, cor }: { texto: string; cor: string }) {
  return (
    <View style={[s.etiqueta, { backgroundColor: cor }]}>
      <Text style={s.etiquetaTexto}>{texto}</Text>
    </View>
  );
}

export function Carregando({ texto = 'Carregando…' }: { texto?: string }) {
  return (
    <View style={s.centro}>
      <ActivityIndicator size="large" color={cores.acento} />
      <Text style={{ marginTop: 12, color: cores.textoSuave }}>{texto}</Text>
    </View>
  );
}

export function Vazio({ texto }: { texto: string }) {
  return (
    <View style={s.centro}>
      <Text style={{ color: cores.textoSuave, textAlign: 'center', fontSize: 16 }}>{texto}</Text>
    </View>
  );
}

export function Titulo({ children }: { children: React.ReactNode }) {
  return <Text style={s.titulo}>{children}</Text>;
}

export function Linha({ rotulo, valor }: { rotulo: string; valor: React.ReactNode }) {
  return (
    <View style={s.linha}>
      <Text style={s.linhaRotulo}>{rotulo}</Text>
      <Text style={s.linhaValor}>{valor}</Text>
    </View>
  );
}

const s = StyleSheet.create({
  botao: { paddingVertical: 16, paddingHorizontal: 20, borderRadius: 12, alignItems: 'center', borderColor: cores.borda, minHeight: 54, justifyContent: 'center' },
  botaoTexto: { fontSize: 17, fontWeight: '700' },
  cartao: { backgroundColor: cores.cartao, borderRadius: 14, padding: 16, marginBottom: 12, borderWidth: 1, borderColor: cores.borda },
  etiqueta: { paddingHorizontal: 10, paddingVertical: 4, borderRadius: 999, alignSelf: 'flex-start' },
  etiquetaTexto: { color: '#fff', fontWeight: '700', fontSize: 12 },
  centro: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: 24 },
  titulo: { fontSize: 22, fontWeight: '800', color: cores.texto, marginBottom: 12 },
  linha: { flexDirection: 'row', justifyContent: 'space-between', paddingVertical: 6, borderBottomWidth: StyleSheet.hairlineWidth, borderColor: cores.borda },
  linhaRotulo: { color: cores.textoSuave },
  linhaValor: { color: cores.texto, fontWeight: '600', flexShrink: 1, textAlign: 'right' },
});
