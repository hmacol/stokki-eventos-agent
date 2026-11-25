// Foto que espera na fila offline. No celular nativo o arquivo já fica no
// disco (a fila guarda o caminho); na web usa IndexedDB (fotoLocal.web.ts).
export async function guardar(uri: string): Promise<string> {
  return uri;
}

export async function liberar(_uri: string): Promise<void> {}

/** Só a versão web precisa ler a foto de volta (o upload nativo lê do disco). */
export async function lerBlob(_uri: string): Promise<Blob | null> {
  return null;
}
