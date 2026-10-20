// Serialise the visible page into compact text for the model.
// Interactive elements get stable refs (data-tw-ref="eN") that the click/type tools resolve.
// Generic: relies only on standard HTML semantics, not on anything specific to the sandbox apps.
(maxChars) => {
  document.querySelectorAll('[data-tw-ref]').forEach(el => el.removeAttribute('data-tw-ref'));
  let n = 0;
  const lines = [];
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'SVG', 'HEAD', 'META', 'LINK']);
  const INTERACTIVE = 'a[href],button,input,select,textarea,[role=button],[role=link],[contenteditable=true]';

  const visible = el => {
    if (el.type === 'hidden') return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const isBlock = el => {
    const d = getComputedStyle(el).display;
    return !d.startsWith('inline') && d !== 'contents' && d !== 'none';
  };

  const labelFor = el => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return clean(l.innerText);
    }
    const wrap = el.closest('label');
    if (wrap) return clean(wrap.innerText);
    return el.getAttribute('placeholder') || el.getAttribute('name') || '';
  };

  const describe = el => {
    const ref = 'e' + (++n);
    el.setAttribute('data-tw-ref', ref);
    const tag = el.tagName;
    if (tag === 'A') return `[${ref}] link "${clean(el.innerText) || el.getAttribute('aria-label') || el.href}"`;
    if (tag === 'BUTTON' || el.getAttribute('role') === 'button') {
      const form = el.form;
      const kind = tag === 'BUTTON' && (el.type || 'submit') === 'submit' && form
        ? ` (submits ${(form.getAttribute('method') || 'get').toUpperCase()} form)` : '';
      return `[${ref}] button "${clean(el.innerText) || el.value || el.getAttribute('aria-label') || ''}"${kind}`;
    }
    if (tag === 'SELECT') {
      const opts = [...el.options].map(o => clean(o.text));
      const cur = el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].text) : '';
      return `[${ref}] select "${labelFor(el)}" selected="${cur}" options=[${opts.map(o => JSON.stringify(o)).join(', ')}]`;
    }
    if (tag === 'TEXTAREA') return `[${ref}] textarea "${labelFor(el)}" value=${JSON.stringify(el.value)}`;
    if (tag === 'INPUT') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'submit' || t === 'button')
        return `[${ref}] button "${el.value}"${el.form ? ` (submits ${(el.form.getAttribute('method') || 'get').toUpperCase()} form)` : ''}`;
      if (t === 'checkbox' || t === 'radio') return `[${ref}] ${t} "${labelFor(el)}" checked=${el.checked}`;
      const val = t === 'password' ? (el.value ? '"••••"' : '""') : JSON.stringify(el.value);
      return `[${ref}] ${t === 'password' ? 'password' : 'textbox'} "${labelFor(el)}" value=${val}`;
    }
    return `[${ref}] ${tag.toLowerCase()} "${clean(el.innerText)}"`;
  };

  // Inline rendering: text with interactive elements described in place.
  const inline = el => {
    const parts = [];
    const walk = node => {
      if (node.nodeType === Node.TEXT_NODE) { const t = clean(node.textContent); if (t) parts.push(t); return; }
      if (node.nodeType !== Node.ELEMENT_NODE || SKIP.has(node.tagName) || !visible(node)) return;
      if (node.matches(INTERACTIVE)) { parts.push(describe(node)); return; }
      if (node.tagName === 'LABEL' && node.htmlFor) return; // rendered as the control's name
      node.childNodes.forEach(walk);
    };
    el.childNodes.forEach(walk);
    return parts.join(' ');
  };

  const emit = (depth, text) => { if (text) lines.push('  '.repeat(Math.min(depth, 6)) + text); };

  const block = (el, depth) => {
    if (SKIP.has(el.tagName) || !visible(el)) return;
    const tag = el.tagName;
    if (/^H[1-6]$/.test(tag)) { emit(depth, '#'.repeat(+tag[1]) + ' ' + inline(el)); return; }
    if (el.matches(INTERACTIVE)) { emit(depth, describe(el)); return; }
    if (tag === 'TABLE') {
      el.querySelectorAll('tr').forEach(tr => {
        if (!visible(tr)) return;
        const cells = [...tr.children].map(c => inline(c));
        emit(depth, '| ' + cells.join(' | ') + ' |');
      });
      return;
    }
    if (tag === 'FORM') {
      emit(depth, `form (${(el.getAttribute('method') || 'get').toUpperCase()} ${el.getAttribute('action') || location.pathname}):`);
      el.childNodes.forEach(c => c.nodeType === 1 ? block(c, depth + 1) : emit(depth + 1, clean(c.textContent)));
      return;
    }
    if (tag === 'LABEL' && el.htmlFor && document.getElementById(el.htmlFor)) return;
    const hasBlockChild = [...el.children].some(c => !SKIP.has(c.tagName) && visible(c) && (isBlock(c) || c.tagName === 'FORM'));
    if (!hasBlockChild) {
      const t = inline(el);
      const role = el.getAttribute('role');
      emit(depth, (role === 'alert' ? '[ALERT] ' : role === 'status' ? '[STATUS] ' : tag === 'LI' ? '- ' : '') + t);
      return;
    }
    el.childNodes.forEach(c => {
      if (c.nodeType === Node.TEXT_NODE) emit(depth, clean(c.textContent));
      else if (c.nodeType === Node.ELEMENT_NODE) block(c, depth + (['UL', 'OL', 'DL'].includes(tag) ? 1 : 0));
    });
  };

  block(document.body, 0);
  // Preserve line breaks inside <pre> blocks (email bodies etc.)
  let text = lines.join('\n');
  document.querySelectorAll('pre').forEach(pre => {
    const flat = clean(pre.innerText);
    if (flat && text.includes(flat)) text = text.replace(flat, '\n' + pre.innerText.trim() + '\n');
  });
  const notices = [...document.querySelectorAll('[role=alert],[role=status],.error,.alert,.flash')]
    .filter(visible).map(e => clean(e.innerText)).filter(Boolean);
  let truncated = false;
  if (text.length > maxChars) { text = text.slice(0, maxChars); truncated = true; }
  return { title: document.title, url: location.href, text, notices: [...new Set(notices)], truncated, refs: n };
}
