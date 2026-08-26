import { Redirect } from 'expo-router';
import { useSessao } from '../src/sessao';
import { Carregando } from '../src/componentes';

export default function Index() {
  const { pronto, motorista } = useSessao();
  if (!pronto) return <Carregando texto="Abrindo…" />;
  return <Redirect href={motorista ? '/(tabs)/rotas' : '/login'} />;
}
