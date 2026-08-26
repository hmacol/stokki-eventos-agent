// Sessão do motorista (contexto React): carrega tokens do SecureStore no
// boot, expõe login/sair, e derruba a sessão quando a API avisa que o
// refresh falhou.
import React, { createContext, useCallback, useContext, useEffect, useState } from 'react';
import * as api from './api';
import type { Motorista } from './tipos';

type Sessao = {
  pronto: boolean;
  motorista: Motorista | null;
  entrar: (cpf: string, pin: string) => Promise<void>;
  sair: () => Promise<void>;
};

const Ctx = createContext<Sessao>({ pronto: false, motorista: null, entrar: async () => {}, sair: async () => {} });

export function ProvedorSessao({ children }: { children: React.ReactNode }) {
  const [pronto, setPronto] = useState(false);
  const [motorista, setMotorista] = useState<Motorista | null>(null);

  useEffect(() => {
    (async () => {
      if (await api.carregarTokens()) {
        try {
          setMotorista(await api.eu());
        } catch (e) {
          // Sem rede: mantém a sessão (tokens ainda podem valer); a API
          // derruba de verdade só quando o refresh falhar com 401.
          if (!(e instanceof api.ErroRede)) setMotorista(null);
          else setMotorista({ cpf: '', nome: 'Motorista', agent_id: null, vehicle_id: null, telefone: null, email: null, tipo_veiculo: null, perfil: 'MOTORISTA' });
        }
      }
      setPronto(true);
    })();
    return api.aoCairSessao(() => setMotorista(null));
  }, []);

  const entrar = useCallback(async (cpf: string, pin: string) => {
    setMotorista(await api.login(cpf.replace(/\D/g, ''), pin));
  }, []);

  const sair = useCallback(async () => {
    await api.limparTokens();
    setMotorista(null);
  }, []);

  return <Ctx.Provider value={{ pronto, motorista, entrar, sair }}>{children}</Ctx.Provider>;
}

export const useSessao = () => useContext(Ctx);
