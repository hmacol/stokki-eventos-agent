// Troca de PIN. Obrigatória quando a operação cadastrou/resetou o PIN
// (motorista.trocar_pin, a Guarda do _layout manda pra cá e não deixa
// sair); opcional pelo Perfil. Sempre 6 dígitos.
import React, { useState } from 'react';
import { KeyboardAvoidingView, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import { useRouter } from 'expo-router';
import { useSessao } from '../src/sessao';
import { Botao } from '../src/componentes';
import { ErroApi, ErroRede } from '../src/api';
import { cores } from '../src/tema';

export default function TrocarPin() {
  const { motorista, trocarPin, sair } = useSessao();
  const router = useRouter();
  const obrigatoria = !!motorista?.trocar_pin;
  const [atual, setAtual] = useState('');
  const [novo, setNovo] = useState('');
  const [confirmar, setConfirmar] = useState('');
  const [erro, setErro] = useState<string | null>(null);
  const [carregando, setCarregando] = useState(false);

  const soDigitos = (v: string) => v.replace(/\D/g, '').slice(0, 6);

  const enviar = async () => {
    setErro(null);
    if (atual.length !== 6) return setErro('Digite o PIN atual (6 dígitos).');
    if (novo.length !== 6) return setErro('O PIN novo precisa ter 6 dígitos.');
    if (novo === atual) return setErro('O PIN novo precisa ser diferente do atual.');
    if (novo !== confirmar) return setErro('A confirmação não confere com o PIN novo.');
    setCarregando(true);
    try {
      await trocarPin(atual, novo);
      router.replace('/(tabs)/rotas');
    } catch (e) {
      if (e instanceof ErroRede) setErro('Sem conexão. Tente de novo quando tiver sinal.');
      else if (e instanceof ErroApi) setErro(e.message);
      else setErro('Não foi possível trocar o PIN.');
    } finally {
      setCarregando(false);
    }
  };

  return (
    <KeyboardAvoidingView behavior="padding" style={s.tela}>
      <ScrollView contentContainerStyle={{ padding: 24, paddingBottom: 40 }} keyboardShouldPersistTaps="handled">
        <Text style={s.titulo}>{obrigatoria ? 'Crie o seu PIN' : 'Trocar PIN'}</Text>
        <Text style={s.texto}>
          {obrigatoria
            ? 'O PIN que você recebeu é provisório. Escolha um PIN de 6 números só seu antes de continuar.'
            : 'Escolha um PIN novo de 6 números.'}
        </Text>

        <Text style={s.rotulo}>PIN atual</Text>
        <TextInput
          style={s.campo} value={atual} onChangeText={(v) => setAtual(soDigitos(v))} keyboardType="number-pad"
          secureTextEntry placeholder="••••••" placeholderTextColor="#9CA3AF" returnKeyType="next"
        />
        <Text style={s.rotulo}>PIN novo (6 dígitos)</Text>
        <TextInput
          style={s.campo} value={novo} onChangeText={(v) => setNovo(soDigitos(v))} keyboardType="number-pad"
          secureTextEntry placeholder="••••••" placeholderTextColor="#9CA3AF" returnKeyType="next"
        />
        <Text style={s.rotulo}>Repita o PIN novo</Text>
        <TextInput
          style={s.campo} value={confirmar} onChangeText={(v) => setConfirmar(soDigitos(v))} keyboardType="number-pad"
          secureTextEntry placeholder="••••••" placeholderTextColor="#9CA3AF" onSubmitEditing={enviar} returnKeyType="go"
        />
        {erro ? <Text style={s.erro}>{erro}</Text> : null}
        <Botao titulo="Salvar PIN" onPress={enviar} carregando={carregando} estilo={{ marginTop: 20 }} />
        <View style={{ marginTop: 12 }}>
          {obrigatoria
            ? <Botao titulo="Sair" tipo="secundario" onPress={() => void sair()} />
            : <Botao titulo="Voltar" tipo="secundario" onPress={() => router.back()} />}
        </View>
        <Text style={s.ajuda}>Esqueceu o PIN atual? Fale com a operação da FreshLog.</Text>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const s = StyleSheet.create({
  tela: { flex: 1, backgroundColor: cores.fundo },
  titulo: { fontSize: 22, fontWeight: '800', color: cores.texto },
  texto: { color: cores.textoSuave, marginTop: 8, lineHeight: 20 },
  rotulo: { color: cores.textoSuave, marginTop: 16, marginBottom: 6, fontWeight: '600' },
  campo: { backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda, borderRadius: 12, padding: 16, fontSize: 20, letterSpacing: 1, color: cores.texto },
  erro: { color: cores.perigo, marginTop: 12, fontWeight: '600' },
  ajuda: { color: cores.textoSuave, textAlign: 'center', marginTop: 24 },
});
