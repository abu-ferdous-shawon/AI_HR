(function () {
  'use strict';
  if (window.__hrAiChatLoaded) { return; }
  window.__hrAiChatLoaded = true;

  // Do not show the widget on Frappe's login page.
  if (window.frappe && frappe.session && frappe.session.user === 'Guest') { return; }

  var BASE = (typeof window.HR_AI_BASE === 'string' ? window.HR_AI_BASE : '/ai').replace(/\/+$/, '');
  var DEV_EMPLOYEE = typeof window.HR_AI_DEV_EMPLOYEE === 'string' ? window.HR_AI_DEV_EMPLOYEE : '';
  var MAX_IMAGES = 3;
  var MAX_CHARS = 1000;

  var CSS = `
  :host { all: initial; }
  .root { --bg:#ffffff; --bot:#eef0f5; --text:#1d2330; --muted:#6b7385; --line:#e3e6ec;
          --accent:#2f5bea; --accent-text:#ffffff; --err-bg:#fdecec; --err:#a32020; }
  .root[data-theme="dark"] { --bg:#1a1d26; --bot:#252a38; --text:#e8eaf0; --muted:#98a0b3;
          --line:#2a2f3c; --accent:#6c8cff; --accent-text:#0e1220; --err-bg:#3a1c1c; --err:#ff9a9a; }
  .root { position:fixed; right:20px; bottom:20px; z-index:1040; color:var(--text);
          font:14px/1.5 system-ui,-apple-system,"Segoe UI","Noto Sans Bengali","Nirmala UI",sans-serif; }
  * { box-sizing:border-box; }
  [hidden] { display:none !important; }
  button, textarea { font:inherit; color:inherit; }
  .fab { width:52px; height:52px; border-radius:50%; border:0; cursor:pointer; font-size:22px;
         background:var(--accent); color:var(--accent-text); box-shadow:0 4px 14px rgba(0,0,0,.25); }
  .panel { position:absolute; right:0; bottom:64px; width:380px; height:min(600px, calc(100vh - 110px));
           display:flex; flex-direction:column; background:var(--bg); border:1px solid var(--line);
           border-radius:14px; box-shadow:0 10px 30px rgba(0,0,0,.28); overflow:hidden; }
  header { display:flex; align-items:center; gap:6px; padding:10px 12px; border-bottom:1px solid var(--line); }
  header h2 { margin:0; font-size:15px; flex:1; }
  header button { border:1px solid var(--line); background:var(--bg); border-radius:8px; padding:3px 9px; cursor:pointer; }
  .msgs { flex:1; overflow-y:auto; padding:12px; display:flex; flex-direction:column; gap:8px; }
  .hint { margin:auto; text-align:center; color:var(--muted); max-width:300px; }
  .msg { max-width:86%; padding:8px 12px; border-radius:14px; white-space:pre-wrap; overflow-wrap:anywhere; }
  .msg.user { align-self:flex-end; background:var(--accent); color:var(--accent-text); border-bottom-right-radius:4px; }
  .msg.bot { align-self:flex-start; background:var(--bot); border-bottom-left-radius:4px; }
  .msg.typing { color:var(--muted); font-style:italic; }
  .msg.error { align-self:center; background:var(--err-bg); color:var(--err); font-size:13px; max-width:94%; }
  .msg img { display:block; max-width:160px; max-height:120px; border-radius:8px; margin-bottom:6px; }
  .msg.bot img { max-width:240px; max-height:240px; margin:8px 0 2px; }
  .caption { font-size:12px; color:var(--muted); }
  .files { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
  .files button { border:1px solid var(--line); background:var(--bg); border-radius:8px; padding:4px 9px; cursor:pointer; }
  .mutation-card { margin-top:10px; padding:10px; border:1px solid var(--line, #d5dae4); border-radius:8px; }
  .mutation-field { margin-top:6px; font-size:.9em; }
  .mutation-status { margin-top:6px; font-size:.9em; }
  .thumbs { display:flex; gap:8px; padding:6px 12px 0; flex-wrap:wrap; }
  .thumb { position:relative; }
  .thumb img { height:48px; border-radius:6px; border:1px solid var(--line); display:block; }
  .thumb button { position:absolute; top:-7px; right:-7px; width:18px; height:18px; padding:0; border-radius:50%;
                  border:1px solid var(--line); background:var(--bg); font-size:11px; line-height:1; cursor:pointer; }
  .composer { display:flex; gap:6px; align-items:flex-end; padding:10px 12px; border-top:1px solid var(--line); }
  .composer textarea { flex:1; resize:none; max-height:110px; padding:8px 10px; border:1px solid var(--line);
                       border-radius:10px; background:var(--bg); }
  .composer button { border:1px solid var(--line); background:var(--bg); border-radius:10px; padding:8px 11px; cursor:pointer; }
  .composer button.send { background:var(--accent); color:var(--accent-text); border-color:var(--accent); }
  button:disabled { opacity:.5; cursor:not-allowed; }
  textarea:focus, button:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
  @media (max-width:480px) {
    .root { right:10px; bottom:10px; }
    .panel { position:fixed; right:0; left:0; bottom:0; top:0; width:auto; height:auto; border-radius:0; }
    .fab { position:relative; z-index:1; }
  }`;

  var HINT = 'Ask about your leave, attendance, holidays, salary slips, profile or company HR policy, ' +
             'in English, বাংলা or Banglish. Ask for a report to get an Excel file, or attach an image with 📎.';

  function el(tag, props, children) {
    var node = document.createElement(tag);
    Object.keys(props || {}).forEach(function (k) {
      if (k === 'text') { node.textContent = props[k]; }
      else if (k === 'class') { node.className = props[k]; }
      else { node.setAttribute(k, props[k]); }
    });
    (children || []).forEach(function (c) { node.appendChild(c); });
    return node;
  }

  function headers(extra) {
    var h = Object.assign({}, extra || {});
    if (DEV_EMPLOYEE) { h['X-Dev-Employee'] = DEV_EMPLOYEE; }   // ignored by the server unless dev mode
    return h;
  }

  var host = el('div', { id: 'hr-ai-chat-host' });
  var shadow = host.attachShadow({ mode: 'open' });
  shadow.appendChild(el('style', { text: CSS }));

  var fab = el('button', { class: 'fab', type: 'button', 'aria-label': 'Open HR Assistant', title: 'HR Assistant', text: '💬' });
  var hint = el('div', { class: 'hint', text: HINT });
  var msgs = el('div', { class: 'msgs', 'aria-live': 'polite' }, [hint]);
  var thumbs = el('div', { class: 'thumbs', hidden: '' });
  var input = el('textarea', { rows: '1', maxlength: String(MAX_CHARS), 'aria-label': 'Your question',
                               placeholder: 'Type your question… (Enter to send)' });
  var attach = el('button', { type: 'button', title: 'Attach images (max 3)', 'aria-label': 'Attach images', text: '📎' });
  var file = el('input', { type: 'file', accept: 'image/*', multiple: '', hidden: '' });
  var sendBtn = el('button', { class: 'send', type: 'button', text: 'Send' });
  var newBtn = el('button', { type: 'button', text: 'New chat' });
  var closeBtn = el('button', { type: 'button', 'aria-label': 'Close', text: '✕' });

  var panel = el('section', { class: 'panel', role: 'dialog', 'aria-label': 'HR Assistant', hidden: '' }, [
    el('header', {}, [el('h2', { text: 'HR Assistant' }), newBtn, closeBtn]),
    msgs, thumbs,
    el('div', { class: 'composer' }, [attach, file, input, sendBtn]),
  ]);
  var root = el('div', { class: 'root' }, [panel, fab]);
  shadow.appendChild(root);

  // Frappe has keyboard shortcuts; keep typing inside the widget from reaching them.
  ['keydown', 'keyup', 'keypress'].forEach(function (t) {
    panel.addEventListener(t, function (e) { e.stopPropagation(); });
  });

  // ---- theme: follow Frappe's theme, else the system ----
  function applyTheme() {
    var t = document.documentElement.getAttribute('data-theme');
    var dark = t === 'dark' || (t !== 'light' && window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches);
    root.setAttribute('data-theme', dark ? 'dark' : 'light');
  }
  applyTheme();
  try {
    new MutationObserver(applyTheme).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  } catch (e) { /* theme stays as set */ }

  // ---- chat state ----
  var convId = null, busy = false, pending = [];

  function scrollDown() { msgs.scrollTop = msgs.scrollHeight; }

  function addMsg(kind, text, images) {
    hint.hidden = true;
    var div = el('div', { class: 'msg ' + kind });
    (images || []).forEach(function (im) {
      div.appendChild(el('img', { src: im.preview, alt: 'attached image' }));
    });
    div.appendChild(document.createTextNode(text));    // text only, never HTML
    msgs.appendChild(div);
    scrollDown();
    return div;
  }

  function addFiles(div, files) {
    var box = el('div', { class: 'files' });
    files.forEach(function (f) {
      var b = el('button', { type: 'button', text: '⬇ ' + f.name });
      b.addEventListener('click', function () { download(f); });
      box.appendChild(b);
    });
    div.appendChild(box);
  }

  async function download(f) {
    try {
      var res = await fetch(BASE + '/reports/' + encodeURIComponent(f.id),
                            { credentials: 'same-origin', headers: headers() });
      if (!res.ok) { addMsg('error', 'This report is no longer available. Please ask for it again.'); return; }
      var url = URL.createObjectURL(await res.blob());
      var a = document.createElement('a');
      a.href = url; a.download = f.name;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 10000);
    } catch (e) { addMsg('error', 'Could not download the report.'); }
  }

  async function showPictures(div, pictures) {
    for (var i = 0; i < pictures.length; i++) {
      var p = pictures[i];
      try {
        var res = await fetch(BASE + '/pictures/' + encodeURIComponent(p.id),
                              { credentials: 'same-origin', headers: headers() });
        if (!res.ok) { throw new Error('not available'); }
        div.appendChild(el('img', { src: URL.createObjectURL(await res.blob()), alt: p.caption || 'picture' }));
        if (p.caption) { div.appendChild(el('div', { class: 'caption', text: p.caption })); }
      } catch (e) {
        div.appendChild(document.createTextNode('\n(The picture could not be loaded.)'));
      }
    }
    scrollDown();
  }

  function setBusy(on) {
    busy = on;
    sendBtn.disabled = on; input.disabled = on; attach.disabled = on;
    if (!on) { input.focus(); }
  }

  function autosize() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 110) + 'px';
  }

  function resetChat() {
    convId = null; pending = []; renderThumbs();
    msgs.replaceChildren(hint);
    hint.hidden = false;
    input.focus();
  }

  // ---- images: shrink in the browser, keep until sent ----
  async function toJpeg(f) {
    var url = URL.createObjectURL(f);
    try {
      var img = await new Promise(function (ok, bad) {
        var i = new Image(); i.onload = function () { ok(i); }; i.onerror = function () { bad(new Error('bad image')); }; i.src = url;
      });
      var scale = Math.min(1, 1600 / Math.max(img.width, img.height));
      var c = document.createElement('canvas');
      c.width = Math.round(img.width * scale); c.height = Math.round(img.height * scale);
      var g = c.getContext('2d');
      g.fillStyle = '#fff'; g.fillRect(0, 0, c.width, c.height);
      g.drawImage(img, 0, 0, c.width, c.height);
      var dataUrl = c.toDataURL('image/jpeg', 0.85);
      return { mime_type: 'image/jpeg', data: dataUrl.split(',')[1], preview: dataUrl };
    } finally { URL.revokeObjectURL(url); }
  }

  async function addImages(files) {
    for (var i = 0; i < files.length; i++) {
      var f = files[i];
      if (pending.length >= MAX_IMAGES) { addMsg('error', 'You can attach up to ' + MAX_IMAGES + ' images.'); break; }
      if (!f.type || f.type.indexOf('image/') !== 0) { continue; }
      try { pending.push(await toJpeg(f)); }
      catch (e) { addMsg('error', 'One image could not be read.'); }
    }
    renderThumbs();
  }

  function renderThumbs() {
    thumbs.replaceChildren();
    pending.forEach(function (im, idx) {
      var x = el('button', { type: 'button', title: 'Remove', 'aria-label': 'Remove image', text: '✕' });
      x.addEventListener('click', function () { pending.splice(idx, 1); renderThumbs(); });
      thumbs.appendChild(el('div', { class: 'thumb' }, [el('img', { src: im.preview, alt: 'to send' }), x]));
    });
    thumbs.hidden = pending.length === 0;
  }

  function detail(data, fallback) {
    return data && typeof data.detail === 'string' ? data.detail : fallback;
  }

  async function send() {
    var text = input.value.trim();
    if (!text && pending.length) { text = 'Please look at the attached image.'; }
    if (!text || busy) { return; }

    var sending = pending;
    pending = []; renderThumbs();
    setBusy(true);
    addMsg('user', text, sending);
    input.value = ''; autosize();
    var typing = addMsg('bot typing', 'Thinking…');

    try {
      var res = await fetch(BASE + '/chat', {
        method: 'POST',
        credentials: 'same-origin',
        headers: headers({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          message: text, conversation_id: convId,
          images: sending.map(function (i) { return { mime_type: i.mime_type, data: i.data }; }),
        }),
      });
      var data = {};
      try { data = await res.json(); } catch (e) { /* not JSON */ }
      typing.remove();

      if (res.ok) {
        convId = data.conversation_id;
        var div = addMsg('bot', data.answer || '');
        if (data.confirmation) {
          try {
            var confirmation = await import(BASE + '/mutation_confirmation.js');
            confirmation.addConfirmation(div, data.confirmation, data.conversation_id, BASE + '/chat/confirm', function () {
              var csrf = window.frappe && frappe.csrf_token;
              return headers(typeof csrf === 'string' ? { 'X-Frappe-CSRF-Token': csrf } : {});
            });
            scrollDown();
          } catch (error) {
            addMsg('error', 'The confirmation card could not load. Start a new proposal.');
          }
        }
        if (data.files && data.files.length) { addFiles(div, data.files); }
        if (data.pictures && data.pictures.length) { showPictures(div, data.pictures); }
      } else if (res.status === 401) {
        addMsg('error', 'Your session has expired. Please refresh the page and sign in again.');
      } else if (res.status === 403) {
        addMsg('error', detail(data, 'Your account is not set up to use the assistant. Please contact HR.'));
      } else if (res.status === 413) {
        addMsg('error', 'An image is too large. Please try a smaller one.');
      } else if (res.status === 422) {
        addMsg('error', detail(data, 'Your message or image could not be accepted.'));
      } else if (res.status === 429) {
        addMsg('error', 'You are sending messages too fast. Please wait a moment.');
      } else if (res.status === 503) {
        addMsg('error', detail(data, 'The assistant is unavailable right now. Please try again.'));
      } else {
        addMsg('error', detail(data, 'Something went wrong. Please try again.'));
      }
    } catch (e) {
      typing.remove();
      addMsg('error', 'Cannot reach the assistant. Check your connection and try again.');
    }
    setBusy(false);
  }

  // ---- events ----
  function toggle(open) {
    panel.hidden = !open;
    fab.setAttribute('aria-label', open ? 'Close HR Assistant' : 'Open HR Assistant');
    if (open) { input.focus(); }
  }
  fab.addEventListener('click', function () { toggle(panel.hidden); });
  closeBtn.addEventListener('click', function () { toggle(false); });
  newBtn.addEventListener('click', resetChat);
  sendBtn.addEventListener('click', send);
  attach.addEventListener('click', function () { file.click(); });
  file.addEventListener('change', function (e) { addImages(Array.prototype.slice.call(e.target.files)); e.target.value = ''; });
  input.addEventListener('input', autosize);
  input.addEventListener('keydown', function (e) {
    // isComposing: do not send while a Bangla/IME word is still being composed
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  input.addEventListener('paste', function (e) {
    var items = (e.clipboardData && e.clipboardData.files) || [];
    var imgs = Array.prototype.filter.call(items, function (f) { return f.type.indexOf('image/') === 0; });
    if (imgs.length) { e.preventDefault(); addImages(imgs); }
  });

  document.body.appendChild(host);
})();