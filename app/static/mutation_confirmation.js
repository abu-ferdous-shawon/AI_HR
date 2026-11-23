const latestCards = new WeakMap();

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function display(value, field) {
  if (value === null || value === '') return 'Empty';
  const states = ['Draft', 'Submitted', 'Cancelled'];
  if (field === 'docstatus' && /^[012]$/.test(String(value))) {
    return states[Number(value)] + ' (' + value + ')';
  }
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}

export function addConfirmation(parent, card, conversationId, endpoint, authHeaders) {
  const messageList = parent.parentElement;
  const latest = latestCards.get(messageList);
  if (latest?.token === card.token) {
    parent.appendChild(latest.section);
    return;
  }

  const section = element('section', undefined, 'mutation-card');
  section.setAttribute('aria-label', 'Proposed Frappe change');
  const name = card.document_name ? ' · ' + card.document_name : '';
  const operation = card.operation[0].toUpperCase() + card.operation.slice(1);
  section.appendChild(element('strong', operation + ' ' + card.doctype + name));

  for (const change of card.changes) {
    const field = element('div', undefined, 'mutation-field');
    field.appendChild(element('strong', change.field));
    if (card.operation !== 'create') {
      field.appendChild(element('div', 'Before · ' + display(change.before, change.field)));
    }
    field.appendChild(element('div', 'After · ' + display(change.after, change.field)));
    section.appendChild(field);
  }

  const status = element('div', undefined, 'mutation-status');
  status.setAttribute('aria-live', 'polite');
  const expires = new Date(card.expires_at);
  if (Number.isFinite(expires.getTime())) {
    status.textContent = 'Expires at ' + expires.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  }
  section.appendChild(status);

  const actions = element('div', undefined, 'files');
  const confirm = element('button', 'Confirm ' + card.operation);
  const discard = element('button', 'Discard');
  confirm.type = discard.type = 'button';
  actions.append(confirm, discard);
  section.appendChild(actions);
  parent.appendChild(section);
  latestCards.set(messageList, { token: card.token, section });

  async function send(decision) {
    if (confirm.disabled) return;
    confirm.disabled = discard.disabled = true;
    status.textContent = decision === 'confirm' ? 'Applying change…' : 'Discarding…';
    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        credentials: 'same-origin',
        redirect: 'error',
        headers: { ...authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversation_id: conversationId, token: card.token, decision }),
      });
      const data = await res.json().catch(() => ({}));
      status.textContent = res.ok ? data.answer : (
        typeof data.detail === 'string' ? data.detail : 'Could not verify the action. Check Frappe before trying again.'
      );
      if (res.status === 429) confirm.disabled = discard.disabled = false;
    } catch (error) {
      status.textContent = 'Connection lost. Check Frappe before making another proposal.';
    }
  }

  confirm.addEventListener('click', () => send('confirm'));
  discard.addEventListener('click', () => send('discard'));
}
