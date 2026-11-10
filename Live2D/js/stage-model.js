/* Load the existing model bundle only after its descriptor is available. */
(() => {
  const requested = new URLSearchParams(location.search).get('model');
  if (requested && /^[\p{L}\p{N}_ -]{1,80}$/u.test(requested)) window.model_name = requested;
  window.__stageModel = { name: window.model_name, state: 'loading' };
  async function load() {
    try {
      const name = encodeURIComponent(window.model_name);
      const response = await fetch(`./live2d-model/${name}/${name}.model3.json`, { signal: AbortSignal.timeout(5000) });
      if (!response.ok) throw new Error(`model descriptor HTTP ${response.status}`);
      await response.json();
      const script = document.createElement('script');
      script.src = './js/bundle.songroom-lipsync.js?v=20261028-pose1';
      script.onload = () => {
        // The existing bundle installs window.onload. Its async insertion can occur
        // after the real load event, so invoke it once and remove the handler.
        const initialize = window.onload;
        window.onload = null;
        try {
          if (typeof initialize === 'function') initialize();
          window.__stageModel.state = 'initialized';
        } catch (error) { failed(error); }
      };
      script.onerror = () => failed(new Error('model bundle unavailable'));
      document.head.appendChild(script);
    } catch (error) { failed(error); }
  }
  function failed(error) {
    window.__stageModel.state = 'unavailable';
    const note = document.createElement('div');
    note.className = 'stage-model-note';
    note.textContent = `模型 ${window.model_name} 暂不可用 · 工位继续运行`;
    note.style.cssText = 'position:absolute;z-index:2;top:50%;left:15%;width:70%;text-align:center;color:#c5dbd6;font:24px sans-serif';
    document.getElementById('stage').appendChild(note);
    console.warn('Stage model unavailable', error.message);
  }
  load();
})();
