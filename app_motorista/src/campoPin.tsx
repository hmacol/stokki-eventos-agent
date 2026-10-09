// Campo de PIN com botão de olho (Hugo, 08/10): o motorista vê o que
// digitou quando quiser. Começa escondido. Usado no login e na troca de PIN.
import React, { useState } from 'react';
import { Pressable, StyleSheet, TextInput, View } from 'react-native';
import type { TextInputProps } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { cores } from './tema';

type Props = Omit<TextInputProps, 'secureTextEntry' | 'onChangeText' | 'value'> & {
  value: string;
  onChangeText: (v: string) => void;
};

export function CampoPin({ value, onChangeText, style, ...resto }: Props) {
  const [visivel, setVisivel] = useState(false);
  return (
    <View style={s.caixa}>
      <TextInput
        {...resto}
        style={[s.campo, style]}
        value={value}
        onChangeText={(v) => onChangeText(v.replace(/\D/g, '').slice(0, 6))}
        keyboardType="number-pad"
        secureTextEntry={!visivel}
        placeholder="••••••"
        placeholderTextColor="#9CA3AF"
        autoComplete="off"
        textContentType="none"
      />
      <Pressable onPress={() => setVisivel((v) => !v)} hitSlop={10} style={s.olho}
        accessibilityRole="button" accessibilityLabel={visivel ? 'Ocultar PIN' : 'Mostrar PIN'}>
        <Ionicons name={visivel ? 'eye-off' : 'eye'} size={24} color={cores.textoSuave} />
      </Pressable>
    </View>
  );
}

const s = StyleSheet.create({
  caixa: { position: 'relative', justifyContent: 'center' },
  campo: { backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda, borderRadius: 12, padding: 16, paddingRight: 52, fontSize: 20, letterSpacing: 1, color: cores.texto },
  olho: { position: 'absolute', right: 14, height: '100%', justifyContent: 'center' },
});
