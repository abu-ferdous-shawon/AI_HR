const STYLES = `
  .reply-content {
    font-family: "Lato", "Trebuchet MS", "Noto Sans Bengali", "Nirmala UI", Arial, sans-serif;
    font-size: var(--reply-size, 1rem);
    font-variant-numeric: tabular-nums;
    line-height: 1.65;
    white-space: normal;
  }
  .reply-content > * { margin: 0; }
  .reply-content > * + * { margin-top: .7em; }
  .reply-content p { white-space: pre-line; }
  .reply-content h3, .reply-content h4 {
    font-size: 1.06em;
    font-weight: 750;
    line-height: 1.35;
  }
  .reply-content h3 { font-size: 1.16em; }
  .reply-content > * + :is(h3, h4) { border-top: 1px solid var(--line); padding-top: .65em; }
  .reply-content :is(h3, h4) + :is(ul, ol) { margin-top: .3em; }
  .reply-content ul, .reply-content ol { padding-inline-start: 1.4em; }
  .reply-content li + li { margin-top: .2em; }
  .reply-content li::marker { color: var(--muted); }
  .reply-content strong { font-weight: 700; }
  .reply-content code { font: .88em ui-monospace, Consolas, monospace; }
  .reply-content code, .reply-content pre { background: var(--reply-inset, var(--bg)); border-radius: 5px; }
  .reply-content :not(pre) > code { padding: .12em .28em; }
  .reply-content pre { overflow-x: auto; padding: .65em .75em; }
  .reply-table { overflow-x: auto; }
  .reply-content table { border-collapse: collapse; font-size: .92em; }
  .reply-content :is(th, td) { padding: .3em .65em; border-bottom: 1px solid var(--line); text-align: start; }
  .reply-content th { background: var(--reply-inset, var(--bg)); }
  .chat-welcome { line-height: 1.55; }
  .chat-welcome strong { display:block; margin-bottom:.25em; font-size:1.08em; }
  .chat-welcome p { margin:0; }
`;

const HEADING = /^(#{1,6})\s+(.+)$/;
const BOLD_LINE = /^(?:\*\*([^*]+)\*\*|__([^_]+)__)(:?)$/;
const LIST_ITEM = /^(?:([-+*•])|(\d{1,6})[.)])\s+(.+)$/;
const FENCE = /^```[\w+-]*$/;

export function installReplyStyles(root) {
  const style = document.createElement('style');
  style.textContent = STYLES;
  root.appendChild(style);
}

export function createWelcome({ messages, hint, endpoint, headers, onSessionEnd }) {
  let request = null;

  async function show() {
    if (request) return;
    const current = new AbortController();
    request = current;
    try {
      const response = await fetch(endpoint, {
        credentials: 'same-origin', headers: headers(), signal: current.signal,
      });
      if (request !== current) return;
      if (!response.ok) {
        request = null;
        if (response.status === 401) onSessionEnd();
        return;
      }
      const data = await response.json();
      if (request !== current) return;
      const greeting = document.createElement('div');
      greeting.className = 'msg bot chat-welcome';
      const name = typeof data.display_name === 'string' ? data.display_name.trim() : '';
      const title = document.createElement('strong');
      title.textContent = name ? 'Hi, ' + name + '!' : 'Welcome!';
      const intro = document.createElement('p');
      intro.textContent = 'I’m your HR assistant. How can I help you today?';
      greeting.append(title, intro);
      hint.hidden = true;
      messages.prepend(greeting);
    } catch {
      if (request === current) request = null;
    }
  }

  function reset() {
    request?.abort();
    request = null;
  }

  return { show, reset };
}

function appendInline(element, text) {
  const markers = /`([^`\n]+)`|\*\*([^*\n]+)\*\*|__([^_\n]+)__/g;
  let end = 0;
  for (const match of text.matchAll(markers)) {
    element.append(text.slice(end, match.index));
    const content = document.createElement(match[1] === undefined ? 'strong' : 'code');
    content.textContent = match[1] ?? match[2] ?? match[3];
    element.appendChild(content);
    end = match.index + match[0].length;
  }
  element.append(text.slice(end));
}

function tableCells(line) {
  return line.includes('|') ? line.replace(/^\|/, '').replace(/\|$/, '').split('|').map(cell => cell.trim()) : [];
}

function addTable(parent, headers, rows) {
  const wrapper = document.createElement('div');
  wrapper.className = 'reply-table';
  const table = document.createElement('table');
  const head = table.createTHead().insertRow();
  for (const label of headers) {
    const cell = document.createElement('th');
    cell.scope = 'col';
    appendInline(cell, label);
    head.appendChild(cell);
  }
  const body = table.createTBody();
  for (const values of rows) {
    const row = body.insertRow();
    for (const value of values) appendInline(row.insertCell(), value);
  }
  wrapper.appendChild(table);
  parent.appendChild(wrapper);
}

// Render the formatting used in chat replies with fixed tags and text nodes.
export function renderReply(text) {
  const reply = document.createElement('div');
  reply.className = 'reply-content';
  const lines = text.replace(/\r\n?/g, '\n').split('\n');
  let paragraph = [];
  let list = null;

  function flushParagraph() {
    if (paragraph.length === 0) return;
    const block = document.createElement('p');
    appendInline(block, paragraph.join('\n'));
    reply.appendChild(block);
    paragraph = [];
  }

  for (let index = 0; index < lines.length; index++) {
    const line = lines[index].trim();
    const item = line.match(LIST_ITEM);
    if (item) {
      flushParagraph();
      const tag = item[2] ? 'OL' : 'UL';
      if (list?.tagName !== tag) {
        list = document.createElement(tag.toLowerCase());
        if (item[2]) list.start = Number(item[2]);
        reply.appendChild(list);
      }
      const entry = document.createElement('li');
      appendInline(entry, item[3]);
      list.appendChild(entry);
      continue;
    }

    list = null;
    const heading = line.match(HEADING);
    const bold = line.match(BOLD_LINE);
    if (!line || heading || bold) {
      flushParagraph();
      if (line) {
        const title = document.createElement(heading && heading[1].length < 3 ? 'h3' : 'h4');
        appendInline(title, heading ? heading[2] : (bold[1] ?? bold[2]) + bold[3]);
        reply.appendChild(title);
      }
      continue;
    }

    if (FENCE.test(line)) {
      let last = index + 1;
      while (last < lines.length && lines[last].trim() !== '```') last++;
      if (last < lines.length) {
        flushParagraph();
        const block = document.createElement('pre');
        const code = document.createElement('code');
        code.textContent = lines.slice(index + 1, last).join('\n');
        block.appendChild(code);
        reply.appendChild(block);
        index = last;
        continue;
      }
    }

    const headers = tableCells(line);
    const divider = headers.length > 1 ? tableCells(lines[index + 1]?.trim() ?? '') : [];
    if (divider.length > 1 && divider.length === headers.length && divider.every(cell => /^:?-{3,}:?$/.test(cell))) {
      flushParagraph();
      const rows = [];
      index++;
      while (index + 1 < lines.length) {
        const cells = tableCells(lines[index + 1].trim());
        if (cells.length !== headers.length) break;
        rows.push(cells);
        index++;
      }
      addTable(reply, headers, rows);
      continue;
    }

    paragraph.push(line);
  }
  flushParagraph();
  return reply;
}
