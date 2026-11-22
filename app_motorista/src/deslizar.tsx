// "Arraste para confirmar" -- ação principal por gesto (Hugo, 26/08: em vez
// de clique, pra evitar toque acidental com o celular no suporte). Sem
// dependência extra: Animated + PanResponder do próprio React Native.
import React, { useRef, useState } from 'react';
import { ActivityIndicator, Animated, PanResponder, Platform, StyleSheet, Text, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { cores } from './tema';

const ALTURA = 64;
const BOTAO = 56;
const MARGEM = 4;
// Sensibilidade (Hugo, 22/11: "ruim, precisa ser mais sensível"): confirma com metade do
// trilho (era 82%). O gesto pega em QUALQUER ponto do trilho, não só na bolinha, e não é
// roubado pela rolagem da tela.
const FRACAO_CONFIRMA = 0.5;

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
  // O PanResponder é criado UMA vez (useRef); se lesse onConfirmar/
  // desabilitado direto das props, ficaria com a versão da 1ª renderização
  // -- foi o bug de 12/09: o checklist validava o formulário VAZIO e
  // dizia "Falta: Nome de quem recebeu" com tudo preenchido. Refs sempre
  // apontam pra versão atual.
  const onConfirmarRef = useRef(onConfirmar);
  onConfirmarRef.current = onConfirmar;
  const bloqueadoRef = useRef(false);
  bloqueadoRef.current = !!desabilitado || ocupado;

  const voltar = () => Animated.spring(x, { toValue: 0, useNativeDriver: false, bounciness: 6 }).start();

  const pan = useRef(
    PanResponder.create({
      onStartShouldSetPanResponder: () => !bloqueadoRef.current,
      // Horizontal manda: gesto mais vertical que horizontal é rolagem da tela, não confirmação.
      onMoveShouldSetPanResponder: (_e, g) => !bloqueadoRef.current && Math.abs(g.dx) > 6 && Math.abs(g.dx) > Math.abs(g.dy) * 1.2,
      // Depois que o arrasto começou, ScrollView/navegador não pode tirar o gesto (cancelaria no meio).
      onPanResponderTerminationRequest: () => false,
      onPanResponderMove: (_e, g) => {
        x.setValue(Math.min(Math.max(0, g.dx), maxRef.current));
      },
      onPanResponderRelease: async (_e, g) => {
        const max = maxRef.current;
        if (max > 0 && g.dx >= max * FRACAO_CONFIRMA) {
          Animated.timing(x, { toValue: max, duration: 120, useNativeDriver: false }).start();
          setOcupado(true);
          bloqueadoRef.current = true;
          try {
            await onConfirmarRef.current();
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
      {...pan.panHandlers}
      style={[s.trilho, { borderColor: cor, opacity: desabilitado ? 0.5 : 1 }, Platform.OS === 'web' ? (SEM_ROLAGEM_HORIZONTAL as object) : null]}
      accessibilityRole="button"
      accessibilityLabel={titulo}
      accessibilityHint="Arraste para a direita para confirmar"
    >
      <Animated.View style={[s.preenchido, { backgroundColor: cor, width: larguraPreenchida, opacity: 0.18 }]} />
      <Animated.Text style={[s.texto, { color: cor, opacity: opacidadeTexto }]} numberOfLines={1}>
        {titulo}  ›››
      </Animated.Text>
      <Animated.View style={[s.botao, { backgroundColor: cor, transform: [{ translateX: x }] }]}>
        {ocupado ? <ActivityIndicator color="#fff" /> : <Ionicons name={icone} size={26} color="#fff" />}
      </Animated.View>
    </View>
  );
}

// Web/PWA (Safari): o navegador só pode rolar na vertical; o horizontal é do gesto de confirmar.
const SEM_ROLAGEM_HORIZONTAL = { touchAction: 'pan-y', userSelect: 'none' };

const s = StyleSheet.create({
  trilho: { height: ALTURA, borderRadius: ALTURA / 2, borderWidth: 2, backgroundColor: '#fff', justifyContent: 'center', overflow: 'hidden' },
  preenchido: { position: 'absolute', left: 0, top: 0, bottom: 0, borderRadius: ALTURA / 2 },
  texto: { position: 'absolute', left: BOTAO + 16, right: 16, textAlign: 'center', fontSize: 16, fontWeight: '800', letterSpacing: 0.5 },
  botao: { position: 'absolute', left: MARGEM, width: BOTAO, height: BOTAO, borderRadius: BOTAO / 2, alignItems: 'center', justifyContent: 'center', elevation: 3 },
});
