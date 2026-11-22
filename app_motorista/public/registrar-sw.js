// Service worker: deixa o app abrir sem sinal (a fila de envios já funciona offline).
if ('serviceWorker' in navigator) {
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function () {});
    // Na 1ª visita os arquivos carregam antes do service worker assumir:
    // manda a lista pra ele guardar (senão o 2º acesso sem sinal não abre).
    var guardar = function () {
      navigator.serviceWorker.ready.then(function (reg) {
        var urls = performance.getEntriesByType('resource').map(function (r) { return r.name; });
        if (reg.active) reg.active.postMessage({ tipo: 'guardar', urls: urls });
      });
    };
    setTimeout(guardar, 3000);
    setTimeout(guardar, 15000);
  });
}
