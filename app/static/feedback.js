// Every page: a button to report a problem / send a suggestion, and errors of the page sent to the admin.
// The page address (path, day, tab) and time are attached; nothing about patients is.
(() => {
  if (window.__orchestraFeedback) return;
  window.__orchestraFeedback = true;
  const here = () => location.pathname + location.search + location.hash;
  const post = (path, body) => fetch(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });

  // ------------------------------------------------------------ errors of the page
  const seen = new Set();
  let sent = 0;
  function report(message, where) {
    message = String(message || '').slice(0, 1000);
    const key = message + '|' + where;
    if (!message || seen.has(key) || sent >= 5) return;
    if (/^Script error\.?$|ResizeObserver loop/i.test(message)) return;  // nothing to go on / harmless
    seen.add(key);
    sent += 1;
    post('/api/client-errors', { message, where: String(where || '').slice(0, 300), page: here() }).catch(() => {});
  }
  window.addEventListener('error', e => {
    if (!e.message) return;
    if (e.filename && !e.filename.startsWith(location.origin)) return;  // browser extensions
    report(e.message, (e.filename || '').replace(location.origin, '') + ':' + (e.lineno || 0));
  });
  window.addEventListener('unhandledrejection', e => {
    const r = e.reason;
    if (r && /กรุณาเข้าสู่ระบบ/.test(r.message || '')) return;  // signed out: not a fault
    report(r && r.message ? r.message : String(r), 'promise');
  });

  // ------------------------------------------------------------ the button and the form
  const css = `
    .fb-open { position: fixed; right: 16px; bottom: 16px; z-index: 40; border: 1px solid var(--border, #d0d7de);
      background: var(--surface, #fff); color: var(--text, #1f2328); border-radius: 99px; padding: 9px 14px;
      font: inherit; font-size: .84rem; cursor: pointer; box-shadow: var(--shadow, 0 4px 14px rgba(0,0,0,.12)); }
    .fb-open:hover { border-color: var(--accent, #0b6e75); }
    .fb-dialog { border: 1px solid var(--border, #d0d7de); border-radius: 14px; padding: 0; width: min(520px, calc(100% - 32px));
      background: var(--surface, #fff); color: var(--text, #1f2328); }
    .fb-dialog::backdrop { background: rgba(0,0,0,.35); }
    .fb-dialog form { display: grid; gap: 12px; padding: 18px; margin: 0; }
    .fb-dialog h2 { margin: 0; font-size: 1.05rem; }
    .fb-dialog p { margin: 0; font-size: .82rem; color: var(--muted, #59636e); }
    .fb-kinds { display: flex; flex-wrap: wrap; gap: 8px; border: 0; padding: 0; margin: 0; }
    .fb-kinds label { display: flex; align-items: center; gap: 6px; border: 1px solid var(--border, #d0d7de); border-radius: 99px;
      padding: 6px 12px; font-size: .84rem; cursor: pointer; }
    .fb-kinds input { margin: 0; }
    .fb-dialog textarea { width: 100%; box-sizing: border-box; min-height: 110px; font: inherit; font-size: .9rem; padding: 10px;
      border: 1px solid var(--border, #d0d7de); border-radius: 10px; background: var(--surface, #fff); color: inherit; resize: vertical; }
    .fb-warn { background: var(--warn-soft, #fef3c7); color: var(--warn, #b45309) !important; border-radius: 8px; padding: 8px 10px; }
    .fb-actions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }
    .fb-actions button { font: inherit; font-size: .88rem; border-radius: 10px; padding: 8px 16px; cursor: pointer;
      border: 1px solid var(--border, #d0d7de); background: var(--surface, #fff); color: inherit; }
    .fb-actions .fb-send { background: var(--accent, #0b6e75); border-color: var(--accent, #0b6e75); color: #fff; }
    .fb-result { font-size: .84rem; border-radius: 8px; padding: 8px 10px; }
    .fb-result.ok { background: var(--ok-soft, #dcfce7); color: var(--ok, #15803d); }
    .fb-result.bad { background: var(--bad-soft, #fee2e2); color: var(--bad, #b91c1c); }
    .fb-legend { font-size: .8rem; color: var(--muted, #59636e); padding: 0; margin-bottom: 6px; }
    .fb-history { font-size: .8rem; color: var(--muted, #59636e); }
    .fb-history summary { cursor: pointer; }
    .fb-history ul { margin: 6px 0 0; padding-left: 18px; display: grid; gap: 4px; }
    .fb-history b { color: var(--text, #1f2328); font-weight: 600; }
    @media (max-width: 600px) { .fb-open { padding: 9px 12px; } .fb-open span { display: none; } }
    @media print { .fb-open, .fb-dialog { display: none !important; } }`;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'fb-open';
  button.setAttribute('aria-label', 'แจ้งปัญหาหรือข้อเสนอแนะ');
  button.innerHTML = '💬 <span>แจ้งปัญหา / ข้อเสนอแนะ</span>';
  const dialog = document.createElement('dialog');
  dialog.className = 'fb-dialog';
  dialog.setAttribute('aria-labelledby', 'fb-title');
  dialog.innerHTML = `<form method="dialog" id="fb-form">
      <h2 id="fb-title">แจ้งปัญหา / ข้อเสนอแนะ</h2>
      <p>ผู้ดูแลระบบจะได้รับเรื่องนี้ ระบบแนบหน้าที่คุณใช้อยู่และเวลาให้เอง</p>
      <fieldset class="fb-kinds"><legend class="fb-legend">เรื่องนี้เป็น</legend>
        <label><input type="radio" name="kind" value="PROBLEM" checked>ปัญหา / ใช้งานไม่ได้</label>
        <label><input type="radio" name="kind" value="IDEA">ข้อเสนอแนะ</label>
        <label><input type="radio" name="kind" value="QUESTION">สอบถาม</label>
      </fieldset>
      <label style="display:grid;gap:6px;font-size:.84rem">เกิดอะไรขึ้น หรืออยากให้ปรับอะไร
        <textarea name="message" maxlength="2000" required placeholder="เช่น กดตรวจสอบช่วงเวลาแล้วขึ้นข้อความผิดพลาด / อยากให้ตารางแสดงชื่อวิสัญญีแพทย์"></textarea></label>
      <p class="fb-warn">อย่าใส่ชื่อผู้ป่วย HN หรือเลขบัตรประชาชน ถ้าเกี่ยวกับเคส ให้บอกรหัสเคส เช่น R-1A2B3C</p>
      <div class="fb-result" hidden></div>
      <div class="fb-actions"><button type="button" class="fb-close">ปิด</button><button type="submit" class="fb-send">ส่งเรื่อง</button></div>
      <details class="fb-history" hidden><summary>เรื่องที่คุณแจ้งล่าสุด</summary><ul></ul></details>
    </form>`;
  const result = dialog.querySelector('.fb-result');
  const show = (text, ok) => { result.hidden = false; result.className = 'fb-result ' + (ok ? 'ok' : 'bad'); result.textContent = text; };
  const STATUS = { NEW: 'รอผู้ดูแลอ่าน', SEEN: 'ผู้ดูแลรับทราบแล้ว', DONE: 'แก้แล้ว / ปิดเรื่อง' };
  function renderMine(list) {
    const box = dialog.querySelector('.fb-history');
    box.hidden = !list || !list.length;
    if (box.hidden) return;
    box.querySelector('ul').innerHTML = list.map(r => `<li>${esc(new Date(r.created_at).toLocaleDateString('th-TH'))} · ${esc(r.kind_label)}:
      ${esc(r.message.length > 60 ? r.message.slice(0, 60) + '…' : r.message)} · <b>${esc(STATUS[r.status] || r.status)}</b>${
      r.admin_note ? ' · ผู้ดูแล: ' + esc(r.admin_note) : ''}</li>`).join('');
  }

  function open() {
    result.hidden = true;
    dialog.showModal();
    dialog.querySelector('textarea').focus();
    fetch('/api/feedback/mine').then(r => r.ok ? r.json() : null).then(d => d && renderMine(d.mine)).catch(() => {});
  }
  button.addEventListener('click', open);
  dialog.querySelector('.fb-close').addEventListener('click', () => dialog.close());
  dialog.querySelector('form').addEventListener('submit', async e => {
    e.preventDefault();
    const form = e.target, send = form.querySelector('.fb-send');
    const body = { kind: form.elements.kind.value, message: form.elements.message.value.trim(), page: here() };
    if (body.message.length < 3) { show('กรุณาเขียนรายละเอียดสั้น ๆ', false); return; }
    send.disabled = true;
    try {
      const res = await post('/api/feedback', body);
      const data = await res.json().catch(() => ({}));
      if (res.status === 401) throw new Error('กรุณาเข้าสู่ระบบก่อนส่งเรื่อง');
      if (!res.ok) throw new Error(data.detail || 'ส่งไม่สำเร็จ ลองใหม่อีกครั้ง');
      form.elements.message.value = '';
      show('ส่งเรื่องแล้ว ขอบคุณครับ/ค่ะ ผู้ดูแลระบบจะตรวจสอบ ดูความคืบหน้าได้ที่ "เรื่องที่คุณแจ้งล่าสุด"', true);
      renderMine(data.mine);
    } catch (err) {
      show(err.message, false);
    } finally {
      send.disabled = false;
    }
  });

  const mount = () => { document.body.append(button, dialog); };
  if (document.body) mount(); else document.addEventListener('DOMContentLoaded', mount);
})();
