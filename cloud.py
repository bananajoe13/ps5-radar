"""PS5-Radar in GitHub Actions: verschluesselter Zustand und verschluesselte Online-Seite.

  python cloud.py pull   state.bin (aus dem Zweig "data") -> ps5.json entschluesseln
  python cloud.py push   ps5.json -> site/state.bin, data.js -> site/data.bin, Seite mit Passwortabfrage bauen

Schutz: Alles, was ins oeffentliche Repository geht, ist mit dem Secret PAGE_PASSWORD verschluesselt
(gzip -> AES-256-GCM, Schluessel per PBKDF2-SHA256, 600.000 Runden). Entschluesselt wird im Browser.
Dateiformat .bin: 16 Byte Salz + 12 Byte IV + Chiffretext.
"""
import gzip
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ROOT = Path(__file__).resolve().parent
SITE = ROOT / 'site'
ITERATIONS = 600_000
MIN_PASSWORD = 12

UNLOCK = """<div id="lock" style="position:fixed;inset:0;z-index:50;background:var(--bg);display:flex;align-items:center;justify-content:center;padding:16px">
  <form id="lockForm" class="tile" style="width:100%;max-width:340px">
    <h1 style="margin:0 0 12px">🎮 PS5-Radar</h1>
    <input type="password" id="lockPw" placeholder="Passwort" autocomplete="current-password" required
      style="width:100%;font:inherit;padding:9px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--text)">
    <label style="display:flex;gap:6px;align-items:center;margin:10px 0;font-size:14px"><input type="checkbox" id="lockRem" checked> Auf diesem Gerät merken</label>
    <button id="lockBtn" type="submit" style="width:100%;font:inherit;padding:9px;border-radius:8px;border:0;background:var(--accent);color:#fff;cursor:pointer">Öffnen</button>
    <div id="lockErr" style="color:var(--bad);margin-top:8px;min-height:1.2em;font-size:14px"></div>
  </form>
</div>
<script>
(async () => {
  const $ = (id) => document.getElementById(id);
  if (!window.crypto || !crypto.subtle || !window.DecompressionStream) { $('lockErr').textContent = 'Dieser Browser ist zu alt.'; return; }
  let buf;
  try { buf = new Uint8Array(await (await fetch('data.bin?t=' + Date.now(), { cache: 'no-store' })).arrayBuffer()); }
  catch (e) { $('lockErr').textContent = 'Daten konnten nicht geladen werden.'; return; }
  const SALT = buf.slice(0, 16), IV = buf.slice(16, 28), CT = buf.slice(28);
  const b64 = (u) => btoa(String.fromCharCode(...new Uint8Array(u)));
  const unb64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
  async function derive(pw) {
    const base = await crypto.subtle.importKey('raw', new TextEncoder().encode(pw), 'PBKDF2', false, ['deriveKey']);
    return crypto.subtle.deriveKey({ name: 'PBKDF2', salt: SALT, iterations: __ITER__, hash: 'SHA-256' }, base, { name: 'AES-GCM', length: 256 }, true, ['decrypt']);
  }
  async function unlock(key) {
    const plain = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: IV }, key, CT);
    const txt = await new Response(new Blob([plain]).stream().pipeThrough(new DecompressionStream('gzip'))).text();
    window.PS5 = JSON.parse(txt);
    $('lock').remove();
    const s = document.createElement('script'); s.textContent = $('app').textContent; document.body.appendChild(s);
  }
  try {  // gemerkt wird nur der abgeleitete Schluessel samt Salz, nicht das Passwort
    const m = JSON.parse(localStorage.getItem('ps5-key') || 'null');
    if (m && m.salt === b64(SALT)) { await unlock(await crypto.subtle.importKey('raw', unb64(m.key), 'AES-GCM', true, ['decrypt'])); return; }
  } catch (e) { try { localStorage.removeItem('ps5-key'); } catch (e2) {} }
  $('lockForm').onsubmit = async (e) => {
    e.preventDefault();
    $('lockBtn').disabled = true; $('lockErr').textContent = '';
    try {
      const key = await derive($('lockPw').value);
      const remember = $('lockRem').checked;
      await crypto.subtle.decrypt({ name: 'AES-GCM', iv: IV }, key, CT);  // wirft bei falschem Passwort
      if (remember) { try { localStorage.setItem('ps5-key', JSON.stringify({ salt: b64(SALT), key: b64(await crypto.subtle.exportKey('raw', key)) })); } catch (e2) {} }
      await unlock(key);
    } catch (err) { if (!$('lock')) throw err; $('lockErr').textContent = 'Falsches Passwort.'; $('lockBtn').disabled = false; $('lockPw').select(); }
  };
  $('lockPw').focus();
})();
</script>"""


def password():
    pw = os.environ.get('PAGE_PASSWORD', '')
    if len(pw) < MIN_PASSWORD:
        sys.exit(f'Secret PAGE_PASSWORD fehlt oder ist kürzer als {MIN_PASSWORD} Zeichen')
    return pw


def key_for(pw, salt):
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS).derive(pw.encode('utf-8'))


def encrypt(pw, data):
    salt, iv = os.urandom(16), os.urandom(12)
    return salt + iv + AESGCM(key_for(pw, salt)).encrypt(iv, gzip.compress(data, 9), None)


def decrypt(pw, blob):
    salt, iv, ct = blob[:16], blob[16:28], blob[28:]
    return gzip.decompress(AESGCM(key_for(pw, salt)).decrypt(iv, ct, None))


def build_page():
    html = (ROOT / 'ps5.html').read_text(encoding='utf-8')
    parts = [
        ('<head>', '<head>\n<meta name="robots" content="noindex,nofollow">\n<meta name="referrer" content="no-referrer">'),
        ('<script src="data.js"></script>\n<script>', UNLOCK.replace('__ITER__', str(ITERATIONS)) + '\n<script type="text/plain" id="app">'),
    ]
    for old, new in parts:
        if html.count(old) != 1:
            raise RuntimeError(f'ps5.html hat sich geändert, Stelle nicht gefunden: {old[:30]}')
        html = html.replace(old, new)
    return html


def pull():
    pw = password()
    src = ROOT / 'state.bin'
    if not src.exists() or not src.stat().st_size:
        print('kein gespeicherter Zustand – erster Lauf')
        return
    (ROOT / 'ps5.json').write_bytes(decrypt(pw, src.read_bytes()))
    print('Zustand entschlüsselt')


def push():
    pw = password()
    SITE.mkdir(exist_ok=True)
    public = (ROOT / 'data.js').read_text(encoding='utf-8').strip()
    public = public[public.index('{'): public.rindex('}') + 1]  # "window.PS5 = {...};" -> JSON
    json.loads(public)  # Plausibilitaetspruefung
    (SITE / 'state.bin').write_bytes(encrypt(pw, (ROOT / 'ps5.json').read_bytes()))
    (SITE / 'data.bin').write_bytes(encrypt(pw, public.encode('utf-8')))
    (SITE / 'index.html').write_text(build_page(), encoding='utf-8')
    (SITE / 'robots.txt').write_text('User-agent: *\nDisallow: /\n', encoding='utf-8')
    (SITE / '.nojekyll').write_text('\n', encoding='utf-8')
    print('Seite gebaut:', sorted(p.name for p in SITE.iterdir()))


if __name__ == '__main__':
    {'pull': pull, 'push': push}[sys.argv[1]]()
