// PWA: o navegador entrega a foto como blob:/data: que morre ao recarregar
// a página, e a fila offline precisa sobreviver a isso. Aqui a foto vai
// pro IndexedDB e a fila guarda só a chave ("idb:<id>").
const BANCO = 'motorista-fotos';
const TABELA = 'fotos';

function abrir(): Promise<IDBDatabase> {
  return new Promise((ok, erro) => {
    const req = window.indexedDB.open(BANCO, 1);
    req.onupgradeneeded = () => req.result.createObjectStore(TABELA);
    req.onsuccess = () => ok(req.result);
    req.onerror = () => erro(req.error ?? new Error('IndexedDB indisponível'));
  });
}

async function transacao<T>(modo: IDBTransactionMode, fn: (t: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await abrir();
  return new Promise<T>((ok, erro) => {
    const req = fn(db.transaction(TABELA, modo).objectStore(TABELA));
    req.onsuccess = () => ok(req.result);
    req.onerror = () => erro(req.error ?? new Error('falha no IndexedDB'));
  });
}

function novoId(): string {
  return `idb:${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/** Copia a foto pro IndexedDB e devolve a chave que vai na fila. */
export async function guardar(uri: string): Promise<string> {
  if (uri.startsWith('idb:')) return uri;
  const blob = await (await fetch(uri)).blob();
  const id = novoId();
  await transacao('readwrite', (t) => t.put(blob, id));
  return id;
}

/** Devolve a foto (ou null se sumiu: o navegador pode limpar o armazenamento). */
export async function lerBlob(uri: string): Promise<Blob | null> {
  try {
    if (uri.startsWith('idb:')) return ((await transacao('readonly', (t) => t.get(uri))) as Blob | undefined) ?? null;
    return await (await fetch(uri)).blob();
  } catch {
    return null;
  }
}

export async function liberar(uri: string): Promise<void> {
  if (!uri.startsWith('idb:')) return;
  try {
    await transacao('readwrite', (t) => t.delete(uri));
  } catch {
    // sobra de espaço, não é erro
  }
}
