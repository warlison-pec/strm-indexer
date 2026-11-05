'use strict';
const form = document.getElementById('loginForm');
const status = document.getElementById('loginStatus');
const button = document.getElementById('loginButton');
const label = button.innerHTML;
const pw = document.getElementById('password');

const say = (text, info = false) => { status.textContent = text; status.classList.toggle('info', info); };

document.getElementById('togglePw').addEventListener('click', e => {
  const show = pw.type === 'password';
  pw.type = show ? 'text' : 'password';
  e.currentTarget.textContent = show ? 'Ocultar' : 'Mostrar';
  e.currentTarget.setAttribute('aria-label', show ? 'Ocultar senha' : 'Mostrar senha');
});

(async () => {
  try {
    const r = await fetch('/auth/me', { cache: 'no-store' });
    if (r.ok) return location.replace('/');
    const p = await r.json();
    if (p.configured === false) say('Nenhum administrador configurado. Rode “python3 auth.py set-admin” no servidor.', true);
  } catch { say('Serviço temporariamente indisponível'); }
})();

form.addEventListener('submit', async e => {
  e.preventDefault();
  const username = document.getElementById('username').value.trim();
  if (username.length < 3 || !pw.value) return say('Informe usuário e senha.');
  button.disabled = true;
  button.innerHTML = '<span class="spinner"></span> Verificando…';
  say('');
  try {
    const r = await fetch('/auth/login', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password: pw.value })
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw Error(data.error || 'Falha no login');
    pw.value = '';
    button.innerHTML = 'Entrando…';
    location.replace('/');
  } catch (err) {
    say(err.message);
    button.disabled = false;
    button.innerHTML = label;
    pw.select();
  }
});
