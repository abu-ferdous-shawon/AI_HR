const RECORDING_LIMIT_MS = 55_000;
const WAV_RATE = 16_000;
const LABELS = {
  idle: 'Voice Input',
  requesting: 'Allow microphone access',
  recording: 'Stop recording',
  transcribing: 'Transcribing…',
};

const STYLES = `
  .voice-control { position:relative; display:flex; flex:none; align-self:flex-end; }
  .composer .voice-button {
    display:grid; place-items:center; width:44px; height:44px; padding:0;
    border:0; border-radius:13px; background:var(--voice-bg, #dedcde); color:var(--voice-color, #211523);
  }
  .composer .voice-button:hover:not(:disabled) { background:var(--voice-hover, #d2cfd3); }
  .composer .voice-button[data-state="recording"] { background:#a51e3f; color:#fff; }
  .composer .voice-button[data-state="recording"]:hover { background:#8b1936; }
  .composer .voice-button:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  .voice-icon { position:relative; width:22px; height:24px; display:block; }
  .voice-icon::before, .voice-icon::after, .voice-icon span { position:absolute; left:50%; transform:translateX(-50%); }
  .voice-icon::before { content:""; top:1px; width:5px; height:10px; border:2px solid; border-radius:6px; }
  .voice-icon::after { content:""; top:8px; width:12px; height:9px; border:2px solid; border-top:0; border-radius:0 0 9px 9px; }
  .voice-icon span { bottom:1px; width:2px; height:5px; border-radius:2px; background:currentColor; }
  .voice-button[data-state="recording"] .voice-icon::before { top:5px; width:13px; height:13px; border:0; border-radius:3px; background:currentColor; }
  .voice-button[data-state="recording"] .voice-icon::after, .voice-button[data-state="recording"] .voice-icon span { display:none; }
  .voice-tooltip {
    position:absolute; z-index:2; bottom:calc(100% + 10px); left:50%; transform:translateX(-50%);
    padding:7px 12px; border-radius:9px; background:#211523; color:#fff;
    font-size:13px; font-weight:500; line-height:1.4; white-space:nowrap;
    opacity:0; pointer-events:none;
  }
  .voice-tooltip::after {
    content:""; position:absolute; top:100%; left:50%; transform:translateX(-50%);
    border:6px solid transparent; border-bottom:0; border-top-color:#211523;
  }
  .voice-control:hover .voice-tooltip, .voice-control:focus-within .voice-tooltip { opacity:1; }
`;

function encodeWav(samples) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  for (const [offset, label] of [[0, 'RIFF'], [8, 'WAVE'], [12, 'fmt '], [36, 'data']]) {
    for (let index = 0; index < label.length; index++) view.setUint8(offset + index, label.charCodeAt(index));
  }
  view.setUint32(4, 36 + samples.length * 2, true);
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, WAV_RATE, true);
  view.setUint32(28, WAV_RATE * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  view.setUint32(40, samples.length * 2, true);
  for (let index = 0; index < samples.length; index++) {
    const sample = Math.max(-1, Math.min(1, samples[index]));
    view.setInt16(44 + index * 2, sample * (sample < 0 ? 32768 : 32767), true);
  }
  return new Blob([buffer], { type: 'audio/wav' });
}

async function toWav(blob) {
  const decoder = new (window.AudioContext || window.webkitAudioContext)();
  let decoded;
  try {
    decoded = await decoder.decodeAudioData(await blob.arrayBuffer());
  } finally {
    await decoder.close();
  }
  const offline = new OfflineAudioContext(1, Math.max(1, Math.ceil(decoded.duration * WAV_RATE)), WAV_RATE);
  const source = offline.createBufferSource();
  source.buffer = decoded;
  source.connect(offline.destination);
  source.start();
  const resampled = await offline.startRendering();
  return encodeWav(resampled.getChannelData(0));
}

function release(stream) {
  stream?.getTracks().forEach(track => track.stop());
}

export function mountVoiceInput({ styleRoot, before, input, endpoint, headers, language = () => 'auto',
                                 canRecord = () => true, onActiveChange, onError }) {
  const style = document.createElement('style');
  style.textContent = STYLES;
  styleRoot.appendChild(style);

  const control = document.createElement('span');
  control.className = 'voice-control';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'voice-button';
  const icon = document.createElement('span');
  icon.className = 'voice-icon';
  icon.setAttribute('aria-hidden', 'true');
  icon.appendChild(document.createElement('span'));
  button.appendChild(icon);
  const tooltip = document.createElement('span');
  tooltip.className = 'voice-tooltip';
  tooltip.setAttribute('aria-hidden', 'true');
  control.append(button, tooltip);
  before.before(control);

  let take = null;
  let chatBusy = false;
  let state = 'idle';

  function update(next) {
    state = next;
    button.dataset.state = state;
    button.setAttribute('aria-label', LABELS[state]);
    button.setAttribute('aria-pressed', String(state === 'recording'));
    button.disabled = chatBusy || state === 'requesting' || state === 'transcribing';
    tooltip.textContent = LABELS[state];
    onActiveChange(state !== 'idle');
  }

  function cancel() {
    if (!take) return;
    const previous = take;
    take = null;
    clearTimeout(previous.timeout);
    previous.abort.abort();
    if (previous.recorder?.state === 'recording') previous.recorder.stop();
    release(previous.stream);
    update('idle');
  }

  function stop(recording) {
    if (take !== recording || state !== 'recording') return;
    clearTimeout(recording.timeout);
    update('transcribing');
    if (recording.recorder.state !== 'inactive') recording.recorder.stop();
    release(recording.stream);
    recording.stream = null;
  }

  async function transcribe(recording) {
    if (take !== recording) return;
    update('transcribing');
    release(recording.stream);
    recording.stream = null;
    try {
      if (!recording.chunks.length) throw new Error('No audio was recorded.');
      const audio = new Blob(recording.chunks, { type: recording.recorder.mimeType });
      const wav = await toWav(audio);
      if (take !== recording) return;
      const response = await fetch(endpoint + '?lang=' + encodeURIComponent(recording.language), {
        method: 'POST', credentials: 'same-origin',
        headers: { ...recording.headers, 'Content-Type': 'audio/wav' },
        body: wav, signal: recording.abort.signal,
      });
      const data = await response.json().catch(() => ({}));
      if (take !== recording) return;
      if (response.ok && typeof data.text === 'string' && data.text.trim()) {
        input.value = [input.value.trim(), data.text.trim()].filter(Boolean).join(' ');
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.focus();
      } else {
        onError(typeof data.detail === 'string' ? data.detail : 'Voice input failed. Please try again.');
      }
    } catch {
      if (take === recording) onError('The recording could not be processed. Please try again.');
    } finally {
      if (take === recording) cancel();
    }
  }

  async function start() {
    if (!canRecord()) return;
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      onError('Voice Input needs a supported browser on HTTPS or localhost.');
      return;
    }
    const recording = { chunks: [], abort: new AbortController(), headers: headers(), language: language() };
    take = recording;
    update('requesting');
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (take !== recording) {
        release(stream);
        return;
      }
      recording.stream = stream;
      const recorder = new MediaRecorder(stream);
      recording.recorder = recorder;
      recorder.addEventListener('dataavailable', event => { if (event.data.size) recording.chunks.push(event.data); });
      recorder.addEventListener('stop', () => transcribe(recording), { once: true });
      recorder.addEventListener('error', () => {
        if (take !== recording) return;
        cancel();
        onError('The microphone stopped recording. Please try again.');
      }, { once: true });
      recorder.start();
      update('recording');
      recording.timeout = setTimeout(() => stop(recording), RECORDING_LIMIT_MS);
    } catch {
      if (take === recording) {
        cancel();
        onError('Microphone access was blocked or the microphone is unavailable.');
      }
    }
  }

  button.addEventListener('click', () => state === 'recording' ? stop(take) : start());
  window.addEventListener('pagehide', cancel);
  update('idle');
  return {
    get active() { return state !== 'idle'; },
    setChatBusy(busy) { chatBusy = busy; update(state); },
    cancel,
  };
}
