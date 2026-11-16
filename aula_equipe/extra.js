// Pecas so da aula da equipe (injetadas depois do overlay.js do cliente).
(() => {
  // "Gerar folha" abre o PDF numa aba nova: no video a folha aparece na propria tela.
  window.open = () => null;
  if (window.top !== window) {
    // tela dentro da moldura de celular: sem o botao "Usar a camera deste computador"
    try { Object.defineProperty(navigator, 'mediaDevices', { value: undefined }); } catch (e) {}
    return;
  }
  window.aulaEquipe = {
    // "visualizador de PDF" com a imagem da folha
    mostrarFolha(url, nome) {
      const v = document.createElement('div');
      v.id = 'aula-folha';
      const barra = document.createElement('div'), papel = document.createElement('div'), img = new Image();
      img.alt = nome || 'folha';
      img.src = url;
      papel.appendChild(img);
      v.append(barra, papel);
      Object.assign(v.style, { position: 'fixed', inset: '0', zIndex: '2147483000', background: '#525659',
                               display: 'flex', flexDirection: 'column', alignItems: 'center' });
      Object.assign(barra.style, { alignSelf: 'stretch', height: '44px', background: '#323639', color: '#f1f1f1',
                                   font: '600 15px Segoe UI, Arial, sans-serif', display: 'flex', alignItems: 'center',
                                   padding: '0 18px' });
      barra.textContent = nome || 'folha.pdf';
      Object.assign(papel.style, { flex: '1', display: 'flex', alignItems: 'flex-start', justifyContent: 'center',
                                   paddingTop: '14px', overflow: 'hidden' });
      Object.assign(img.style, { height: 'calc(100vh - 70px)', boxShadow: '0 4px 18px rgba(0,0,0,.45)', background: '#fff' });
      document.body.appendChild(v);
    },
    fecharFolha() {
      const v = document.getElementById('aula-folha');
      if (v) v.remove();
    },
  };
})();
