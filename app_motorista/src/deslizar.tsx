// "Arraste para confirmar" -- ação principal por gesto (Hugo, 26/08: em vez
// de clique, pra evitar toque acidental com o celular no suporte). Sem
// dependência extra: Animated + PanResponder do próprio React Native.
import React, { useRef, useState } from 'react';
import { ActivityIndicator, Animated, PanResponder, StyleSheet, Text, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { cores } from './tema';

const ALTURA = 64;
const BOTAO = 56;
const MARGEM = 4;

export function Deslizar({
  titulo, onConfirmar, cor = cores.acento, desabilitado, icone = 'arrow-forward',
}: {
  titulo: string;
  onConfirmar: () => Promise<void> | void;
  cor?: string;
  desabilitado?: boolean;
  icone?: React.ComponentProps<typeof Ionicons>['name'];
}) {
  const [largura, setLargura] = useState(0);
  const [ocupado, setOcupado] = useState(false);
  const x = useRef(new Animated.Value(0)).current;
  const maxRef = useRef(0);
  maxRef.current = Math.max(0, largura - BOTAO - MARGEM * 2);

  const voltar = () => Animated.spring(x, { toValue: 0, useNativeDriver: false, bounciness: 6 }).start();

  const pan = useRef(
    PanResponder.create({
      onStartShouldSetPanResponder: () => !desabilitado && !ocupado,
      onMoveShouldSetPanResponder: (_e, g) => !desabilitado && !ocupado && Math.abs(g.dx) > 4,
      onPanResponderMove: (_e, g) => {
        x.setValue(Math.min(Math.max(0, g.dx), maxRef.current));
      },
      onPanResponderRelease: async (_e, g) => {
        const max = maxRef.current;
        if (max > 0 && g.dx >= max * 0.82) {
          Animated.timing(x, { toValue: max, duration: 120, useNativeDriver: false }).start();
          setOcupado(true);
          try {
            await onConfirmar();
          } finally {
            setOcupado(false);
            x.setValue(0);
          }
        } else {
          voltar();
        }
      },
      onPanResponderTerminate: voltar,
    }),
  ).current;

  const opacidadeTexto = x.interpolate({ inputRange: [0, Math.max(1, maxRef.current * 0.6)], outputRange: [1, 0], extrapolate: 'clamp' });
  const larguraPreenchida = Animated.add(x, new Animated.Value(BOTAO + MARGEM * 2));

  return (
    <View
      onLayout={(e) => setLargura(e.nativeEvent.layout.width)}
      style={[s.trilho, { borderColor: cor, opacity: desabilitado ? 0.5 : 1 }]}
      accessibilityRole="button"
      accessibilityLabel={titulo}
      accessibilityHint="Arraste para a direita para confirmar"
    >
      <Animated.View style={[s.preenchido, { backgroundColor: cor, width: larguraPreenchida, opacity: 0.18 }]} />
      <Animated.Text style={[s.texto, { color: cor, opacity: opacidadeTexto }]} numberOfLines={1}>
        {titulo}  ›››
      </Animated.Text>
      <Animated.View {...pan.panHandlers} style={[s.botao, { backgroundColor: cor, transform: [{ translateX: x }] }]}>
        {ocupado ? <ActivityIndicator color="#fff" /> : <Ionicons name={icone} size={26} color="#fff" />}
      </Animated.View>
    </View>
  );
}

const s = StyleSheet.create({
  trilho: { height: ALTURA, borderRadius: ALTURA / 2, borderWidth: 2, backgroundColor: '#fff', justifyContent: 'center', overflow: 'hidden' },
  preenchido: { position: 'absolute', left: 0, top: 0, bottom: 0, borderRadius: ALTURA / 2 },
  texto: { position: 'absolute', left: BOTAO + 16, right: 16, textAlign: 'center', fontSize: 16, fontWeight: '800', letterSpacing: 0.5 },
  botao: { position: 'absolute', left: MARGEM, width: BOTAO, height: BOTAO, borderRadius: BOTAO / 2, alignItems: 'center', justifyContent: 'center', elevation: 3 },
});
