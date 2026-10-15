document.addEventListener('DOMContentLoaded', () => {
  window.lucide?.createIcons({ attrs: { 'stroke-width': 1.8 } });
  const workspace = new URLSearchParams(window.location.search).get('workspace');
  if (workspace) {
    document.querySelectorAll('a[href], form[action]').forEach((element) => {
      const attribute = element.tagName === 'FORM' ? 'action' : 'href';
      const value = element.getAttribute(attribute);
      if (!value || value.startsWith('#') || value.startsWith('mailto:')) return;
      const url = new URL(value, window.location.origin);
      if (url.origin !== window.location.origin) return;
      url.searchParams.set('workspace', workspace);
      element.setAttribute(attribute, `${url.pathname}${url.search}${url.hash}`);
    });
  }
  document.querySelectorAll('[data-dropzone]').forEach((dropzone) => {
    const input = dropzone.querySelector('input[type="file"]');
    const label = dropzone.querySelector('[data-file-label]');
    const help = dropzone.querySelector('[data-file-help]');
    const button = dropzone.querySelector('[data-file-button]');
    const showFile = (file) => {
      if (!file) return;
      label.textContent = file.name;
      help.textContent = `${Math.max(1, Math.ceil(file.size / 1024))} KB selected — ready to generate questions`;
      button.textContent = 'Change file';
      dropzone.classList.add('has-file');
    };
    input.addEventListener('change', () => showFile(input.files[0]));
    ['dragenter', 'dragover'].forEach((eventName) => dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.add('is-dragging');
    }));
    ['dragleave', 'drop'].forEach((eventName) => dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.remove('is-dragging');
    }));
    dropzone.addEventListener('drop', (event) => {
      const file = event.dataTransfer.files[0];
      if (!file) return;
      const transfer = new DataTransfer();
      transfer.items.add(file);
      input.files = transfer.files;
      showFile(file);
    });
  });
  const timer = document.querySelector('.timer[data-minutes]');
  if (!timer) return;
  let seconds = Number(timer.dataset.minutes) * 60;
  const display = timer.querySelector('strong');
  const tick = () => {
    const min = Math.floor(seconds / 60).toString().padStart(2, '0');
    const sec = (seconds % 60).toString().padStart(2, '0');
    display.textContent = `${min}:${sec}`;
    if (seconds <= 0) { document.getElementById('exam-form')?.requestSubmit(); return; }
    seconds -= 1; setTimeout(tick, 1000);
  };
  tick();
});
