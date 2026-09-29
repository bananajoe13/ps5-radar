"""PS5-Radar: sucht PS5-Angebote (mit Laufwerk) im DACH-Raum und meldet neue Angebote unter der Preisgrenze.

Aufruf:
  ps5.py              Abruf jetzt
  ps5.py --if-stale   nur abrufen, wenn der letzte Abruf aelter als stale_hours ist
  ps5.py --claude     wie --if-stale, gibt danach die noch nicht per Claude gemeldeten Alarme als JSON aus
"""
import argparse
import base64
import html
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
# Zugangsdaten nie in config.json (die liegt oeffentlich auf GitHub): lokal secrets.json, in GitHub Actions Secrets/Umgebung
_SECRETS = json.loads((ROOT / 'secrets.json').read_text(encoding='utf-8')) if (ROOT / 'secrets.json').exists() else {}
for _k in ('serpapi_key', 'ntfy_topic', 'ebay_client_id', 'ebay_client_secret'):
    CFG[_k] = os.environ.get(_k.upper()) or _SECRETS.get(_k) or ''
IN_CLOUD = bool(os.environ.get('GITHUB_ACTIONS'))
STATE_FILE = ROOT / 'ps5.json'
DATA_JS = ROOT / 'data.js'
LOG = ROOT / 'ps5.log'
NO_NOTIFY = '--no-notify' in sys.argv
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36'
HEADERS = {'User-Agent': UA, 'Accept-Language': 'de-DE,de;q=0.9',
           'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}


def log(msg):
    line = f'{datetime.now():%Y-%m-%d %H:%M:%S} {msg}'
    print(line)
    with LOG.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def get(url, **kw):
    r = requests.get(url, headers=HEADERS, timeout=40, **kw)
    r.raise_for_status()
    return r.text


def txt(s):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', s or ''))).strip()


def num(s):
    """'1.234,56 €' / '449.-' / '539€' / 599.99 -> float"""
    if isinstance(s, (int, float)):
        return float(s)
    s = re.sub(r'[^\d,.]', '', s or '')
    if not s:
        return None
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.') if s.rfind(',') > s.rfind('.') else s.replace(',', '')
    elif ',' in s:
        s = s.replace(',', '.')
    elif re.fullmatch(r'\d{1,3}(\.\d{3})+', s):
        s = s.replace('.', '')
    s = s.rstrip('.')
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------- Filter

ACCESSORY = re.compile(
    r'st[äa]nder|standfu|halterung|l[üu]fter|k[üu]hl|ladestation|aufkleber|sticker|skin|faceplate|cover|abdeck|'
    r'h[üu]lle|staubschutz|tasche|case\b|festplatte|speichererweiterung|kabel|headset|\bfuss\b|'
    r'laufwerk f[üu]r|disc-laufwerk \[|f[üu]r (?:die )?(?:ps5|playstation)|kompatibel|remote player|portal|'
    r'controller f[üu]r|nur controller|ersatz|reparatur|defekt|bastler|spiel(?:e)? f[üu]r|game key|\bgame\b|'
    r'blu-ray disc\)|\| disc \||plus abo|ps plus|playstation plus|guthaben|vr2|vr 2|'
    r'vertrag|tarif|allnet|/monat|gigakombi|modifi|jailbreak|custom firmware', re.I)
CONSOLE = re.compile(r'konsole|console|disc|disk|laufwerk|slim|standard|1 ?tb|825 ?gb|modellgruppe|bundle', re.I)
PS5 = re.compile(r'playstation\s*®?\s*5|\bps\s?5\b', re.I)
USED = re.compile(r'gebraucht|refurbished|refurb|generalüberholt|b-ware|wie neu|sehr gut|zustand|used|occasion|renewed', re.I)


def is_ps5_with_drive(title):
    t = title or ''
    if not PS5.search(t) or not CONSOLE.search(t) or ACCESSORY.search(t):
        return False
    has_drive = re.search(r'disc|disk|laufwerk|standard', t, re.I)
    if re.search(r'digital|digi\.', t, re.I) and not has_drive:
        return False
    if re.search(r'\bpro\b', t, re.I) and not re.search(r'laufwerk', t, re.I):
        return False
    return True


# ---------------------------------------------------------------- Quellen
# Jede Quelle liefert eine Liste von Angeboten:
# {id, source, shop, country, title, price, currency, condition ('neu'|'gebraucht'), url, kind ('shop'|'deal'), posted?, note?}

def offer(**kw):
    kw.setdefault('condition', 'neu')
    kw.setdefault('kind', 'shop')
    kw.setdefault('currency', 'EUR')
    kw.setdefault('note', '')
    return kw


def pepper_feed(url, source, country, base):
    """mydealz.de / preisjaeger.at: Deals der Community (RSS)."""
    t = get(url)
    out = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=CFG['deal_days'])
    for it in re.findall(r'<item>(.*?)</item>', t, re.S):
        title = txt(re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', re.search(r'<title>(.*?)</title>', it, re.S).group(1)))
        link = txt(re.search(r'<link>(.*?)</link>', it, re.S).group(1))
        pub = parsedate_to_datetime(re.search(r'<pubDate>(.*?)</pubDate>', it).group(1))
        if pub < cutoff or not is_ps5_with_drive(title):
            continue
        tag = re.search(r'<pepper:merchant[^>]*>', it)
        tag = tag.group(0) if tag else ''
        shop = html.unescape((re.search(r'name="([^"]*)"', tag) or [None, 'unbekannt'])[1])
        p = num((re.search(r'price="([^"]*)"', tag) or [None, ''])[1])
        desc = txt(re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', (re.search(r'<description>(.*?)</description>', it, re.S) or [None, ''])[1]))
        eff = effective_price(title, desc, p)
        if not p:  # kein Preis im Haendler-Feld: Preis aus dem Titel ("... für 499€")
            pm = re.search(r'(\d{3}(?:[.,]\d{2})?)\s*€', title)
            p = num(pm.group(1)) if pm else None
        if not p:
            continue
        notes = []
        if re.search(r'lokal', title, re.I):
            notes.append('Lokaler Deal')
        if eff:
            notes.append(f'effektiv mit Gutschein/Cashback – Listenpreis {p:.2f} €'.replace('.', ','))
        out.append(offer(id=link.rstrip('/').rsplit('-', 1)[-1], source=source, shop=shop, country=country,
                         title=title, price=eff or p, url=link, kind='deal', posted=pub.astimezone().isoformat(timespec='seconds'),
                         condition='gebraucht' if USED.search(title) else 'neu', note=' · '.join(notes),
                         expired=deal_expired(link)))
    return out


def deal_expired(link):
    """mydealz/preisjaeger: Deal-Seite enthaelt "isExpired":true/false. None = unbekannt (Seite nicht lesbar)."""
    try:
        m = re.search(r'"isExpired"\s*:\s*(true|false)', get(link))
    except requests.RequestException:
        return None
    return (m.group(1) == 'true') if m else None


def effective_price(title, desc, list_price):
    """Endpreis nach Gutschein/Cashback aus Deal-Titel oder -Text, z. B. '= 585,30€ effektiv' oder 'effektiv nur 579 €'."""
    pats = (r'=\s*([\d.]+,\d{2}|\d{3,4})\s*€?\s*(?:effektiv|eff\.)',
            r'([\d.]+,\d{2}|\d{3,4})\s*€\s*(?:effektiv|eff\.)',
            r'effektiv(?:er Preis)?[:\s]+(?:nur\s+|von\s+|für\s+)?([\d.]+,\d{2}|\d{3,4})\s*€')
    for text in (title, desc[:1500]):
        for pat in pats:
            m = re.search(pat, text, re.I)
            v = num(m.group(1)) if m else None
            # plausibel: Konsolenpreis und guenstiger als der Listenpreis
            if v and 300 <= v and (not list_price or v < list_price):
                return v
    return None


def mydealz():
    return pepper_feed('https://www.mydealz.de/rss/gruppe/playstation-5', 'mydealz', 'DE', 'https://www.mydealz.de')


def preisjaeger():
    return pepper_feed('https://www.preisjaeger.at/rss/gruppe/playstation-5', 'preisjaeger', 'AT', 'https://www.preisjaeger.at')


def preispirat():
    """preispirat.ch: Schweizer Deal-Seite (WordPress-Feed, Preise in CHF)."""
    t = get('https://www.preispirat.ch/?s=playstation+5&feed=rss2')
    out = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=CFG['deal_days'])
    for it in re.findall(r'<item>(.*?)</item>', t, re.S):
        title = txt(re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', re.search(r'<title>(.*?)</title>', it, re.S).group(1)))
        link = txt(re.search(r'<link>(.*?)</link>', it, re.S).group(1))
        pub = parsedate_to_datetime(re.search(r'<pubDate>(.*?)</pubDate>', it).group(1))
        if pub < cutoff or not is_ps5_with_drive(title):
            continue
        desc = txt(re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', (re.search(r'<description>(.*?)</description>', it, re.S) or [None, ''])[1]))
        m = re.search(r'(?:CHF|Fr\.)\s*([\d\'.,]+)', title + ' ' + desc) or re.search(r'([\d\'.,]{3,})\s*(?:CHF|Fr\.|\.-|.–)', title + ' ' + desc)
        p = num(m.group(1).replace("'", '')) if m else None
        if not p:
            continue
        out.append(offer(id=link, source='preispirat', shop='preispirat.ch', country='CH', title=title, price=p,
                         currency='CHF', url=link, kind='deal', posted=pub.astimezone().isoformat(timespec='seconds')))
    return out


def amazon():
    # Amazon blockiert GitHub-Server: in der Cloud kommen die Preise vom PC (--amazon-relay) ueber ntfy
    return amazon_from_relay() if IN_CLOUD else amazon_scrape()


def relay_topic():
    return CFG['ntfy_topic'] + '-amazon'  # eigener Kanal, das Handy abonniert nur den Hauptkanal


def amazon_relay_send():
    """PC: Amazon abfragen und das Ergebnis fuer die Cloud ablegen (ntfy speichert Nachrichten ca. 12 Std.)."""
    if not CFG.get('ntfy_topic'):
        sys.exit('ntfy_topic fehlt in secrets.json')
    offers = [{**o, 'title': o['title'][:120]} for o in amazon_scrape()][:10]
    requests.post('https://ntfy.sh/', timeout=30, json={
        'topic': relay_topic(), 'message': json.dumps({'at': now_iso(), 'offers': offers}, ensure_ascii=False)}).raise_for_status()
    log(f'Amazon-Relay: {len(offers)} Angebote an die Cloud übergeben')


def amazon_from_relay():
    r = requests.get(f'https://ntfy.sh/{relay_topic()}/json', params={'poll': '1', 'since': '12h'}, timeout=30)
    r.raise_for_status()
    msgs = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    msgs = [m for m in msgs if m.get('event') == 'message' and m.get('message', '').startswith('{')]
    if not msgs:
        raise RuntimeError('keine Amazon-Daten vom PC in den letzten 12 Std. (PC aus?)')
    return json.loads(msgs[-1]['message'])['offers']


def amazon_get():
    """Wie ein Browser: erst Startseite (Cookies), dann Suche; bei 503 (Amazon bremst) kurz warten, max. 3 Versuche."""
    h = {**HEADERS, 'Accept-Encoding': 'gzip, deflate', 'Upgrade-Insecure-Requests': '1',
         'Sec-Fetch-Dest': 'document', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Site': 'none', 'Sec-Fetch-User': '?1'}
    for attempt in range(3):
        s = requests.Session()
        s.headers.update(h)
        s.get('https://www.amazon.de/', timeout=30)
        time.sleep(2)
        r = s.get('https://www.amazon.de/s?k=playstation+5+slim+disc&i=videogames', timeout=40,
                  headers={'Referer': 'https://www.amazon.de/', 'Sec-Fetch-Site': 'same-origin'})
        if r.status_code == 200 and 'data-asin' in r.text:
            return r.text
        time.sleep(20)
    r.raise_for_status()
    return r.text


def amazon_scrape():
    t = amazon_get()
    if 'data-asin' not in t:
        raise RuntimeError('Bot-Prüfung von Amazon – später wieder')
    out = []
    starts = [m.start() for m in re.finditer(r'<div role="listitem" data-asin="', t)]
    for i, s in enumerate(starts):
        b = t[s: starts[i + 1] if i + 1 < len(starts) else s + 30000]
        asin = re.search(r'data-asin="(\w+)"', b).group(1)
        tm = re.search(r'<h2[^>]*aria-label="([^"]+)"', b) or re.search(r'<h2.*?<span[^>]*>([^<]+)</span>', b, re.S)
        pm = re.search(r'class="a-price"[^>]*><span class="a-offscreen">([^<]+)<', b)
        if not tm or not pm or 'Gesponsert' in b[:3000] or re.search(r'Derzeit nicht verfügbar|Nicht auf Lager', b):
            continue
        title = txt(tm.group(1))
        if not is_ps5_with_drive(title):
            continue
        cond = 'gebraucht' if USED.search(title) or 'Renewed' in b else 'neu'
        out.append(offer(id=asin, source='amazon', shop='Amazon.de', country='DE', title=title, price=num(pm.group(1)),
                         url=f'https://www.amazon.de/dp/{asin}', condition=cond))
    return out


def mediamarkt_like(domain, shop, sales_line):
    """Kategorieseite "PlayStation 5 Konsolen" + Suche; Produktdaten stecken im __PRELOADED_STATE__."""
    ap = {}
    for path in ('/de/category/playstation-5-konsolen-7857.html', f'/de/search.html?query={quote("playstation 5 slim")}'):
        try:
            t = get(f'https://www.{domain}{path}')
        except requests.RequestException:
            continue
        i = t.find('__PRELOADED_STATE__ = ')
        if i < 0:
            continue
        raw = re.sub(r'(?<=[:\[,])undefined(?=[,\]}])', 'null', t[i + len('__PRELOADED_STATE__ = '):])
        state, _ = json.JSONDecoder().raw_decode(raw)
        ap.update(state.get('apolloState') or {})
    if not ap:
        raise RuntimeError('keine Produktdaten')
    out = []
    for k, v in ap.items():
        if not k.startswith('GraphqlProduct:'):
            continue
        pid = v.get('id')
        title = v.get('title') or ''
        if not is_ps5_with_drive(title):
            continue
        url = f'https://www.{domain}' + (v.get('url') or '')
        price = next((x for kk, x in ap.items() if kk.startswith('CofrPriceFeature:') and f':{pid}' in kk and x.get('price')), None)
        status = next((x for kk, x in ap.items() if kk.startswith('CofrOnlineStatusFeature:') and kk.endswith(f':{pid}')), {})
        available = status.get('isAvailableForDelivery', True) and status.get('onlineStatus', 'AVAILABLE') in ('AVAILABLE', 'MP_OFFER')
        if price and price['price'].get('amount') and available:
            if not price.get('isProductOfTypeMarketplace'):
                out.append(offer(id=pid, source=domain.split('.')[0], shop=shop, country='DE', title=title,
                                 price=price['price']['amount'], url=url))
            else:
                seller = (price.get('marketplaceSeller') or {}).get('name', 'Marktplatz')
                out.append(offer(id=pid, source=domain.split('.')[0], shop=f'{shop} ({seller})', country='DE',
                                 title=title, price=price['price']['amount'], url=url, note='Marktplatz-Händler'))
        refurb = next((x for kk, x in ap.items() if kk.startswith('CofrRefurbishedGoodsFeature:') and kk.endswith(f':{pid}')), None)
        ro = (refurb or {}).get('offer') or {}
        if ro.get('priceAmount'):
            cond = {'LIKE_NEW': 'wie neu', 'VERY_GOOD': 'sehr gut', 'GOOD': 'gut'}.get(ro.get('conditionType'), ro.get('conditionType', ''))
            out.append(offer(id=f"{pid}-r{ro.get('offerId')}", source=domain.split('.')[0], shop=shop, country='DE',
                             title=title, price=ro['priceAmount'], url=url, condition='gebraucht',
                             note=f'Refurbished „{cond}“, mit Gewährleistung'))
    return out


def mediamarkt():
    return mediamarkt_like('mediamarkt.de', 'MediaMarkt', 'Media')


def saturn():
    return mediamarkt_like('saturn.de', 'Saturn', 'Saturn')


def otto():
    out = []
    for q in ('playstation 5 disk', 'ps5 slim konsole', 'playstation 5 konsole'):
        t = get(f'https://www.otto.de/suche/{quote(q)}/')
        for b in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', t, re.S):
            try:
                j = json.loads(b)
            except ValueError:
                continue
            if j.get('@type') != 'Product' or not is_ps5_with_drive(j.get('name', '')):
                continue
            for o in j.get('offers') or []:
                if 'InStock' not in (o.get('availability') or 'InStock'):
                    continue
                vid = next((p['value'] for p in j.get('additionalProperty', []) if p.get('name') == 'variationId'), j['url'])
                out.append(offer(id=vid, source='otto', shop='OTTO', country='DE', title=j['name'], price=num(o['price']),
                                 url='https://www.otto.de' + o.get('url', j['url']),
                                 condition='neu' if 'NewCondition' in o.get('itemCondition', 'New') else 'gebraucht'))
    return out


def billiger():
    """billiger.de: passende Produkte suchen, dann auf jeder Produktseite ALLE Haendlerangebote lesen (JSON-LD)."""
    t = get('https://www.billiger.de/search?searchstring=' + quote('playstation 5 slim disc'))
    products = {}
    for m in re.finditer(r'href="(/products/(\d+)-[^"?]+)[^"]*"[^>]*title="([^"]+)"', t):
        if is_ps5_with_drive(txt(m.group(3))):
            products.setdefault(m.group(2), (m.group(1), txt(m.group(3))))
    out = []
    for pid, (path, title) in list(products.items())[:6]:
        page = get('https://www.billiger.de' + path)
        for b in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S):
            try:
                j = json.loads(b)
            except ValueError:
                continue
            agg = j.get('offers') if isinstance(j, dict) else None
            if not isinstance(agg, dict):
                continue
            for o in agg.get('offers') or []:
                seller = ((o.get('seller') or {}).get('name') or '').strip()
                if not seller or not o.get('price'):
                    continue
                out.append(offer(id=f'{pid}|{seller}', source='billiger', shop=seller, country='DE', title=title,
                                 price=num(o['price']), url='https://www.billiger.de' + path,
                                 condition='neu' if 'NewCondition' in (o.get('itemCondition') or 'New') else 'gebraucht',
                                 note='über billiger.de'))
    return out


def rebuy():
    """rebuy.de: gebraucht mit rebuy-Garantie (Dauer bitte beim Kauf prüfen)."""
    t = get('https://www.rebuy.de/kaufen/konsolen-und-zubehoer/playstation/playstation-5/konsolen')
    links = {txt(m.group(3)): (m.group(2), m.group(1))
             for m in re.finditer(r'href="(/i,(\d+)/playstation-5/[^"]+)"\s+title="([^"]+)"', t)}
    out = []
    # Kachel-Text: "Zustand wählen [Sale] <Name> PlayStation 5 ★★★★★ ... Auf Lager ab 613,99 €"
    for m in re.finditer(r'(?:Zustand wählen|In den Warenkorb)\s+(?:Sale\s+)?([^★€]{5,200}?)\s+PlayStation 5\s+★([^€]{0,300}?)ab\s*([\d.]+,\d{2})\s*€', txt(t)):
        title = m.group(1).strip()
        if title not in links or not is_ps5_with_drive(title) or re.search(r'ausverkauft|nicht (?:auf Lager|verfügbar)', m.group(2), re.I):
            continue
        rid, path = links[title]
        out.append(offer(id=rid, source='rebuy', shop='rebuy', country='DE', title=title, price=num(m.group(3)),
                         url='https://www.rebuy.de' + path, condition='gebraucht', note='gebraucht mit rebuy-Garantie'))
    return out


def mueller():
    """mueller.de: Suchergebnis enthaelt eine JSON-LD-Produktliste (ohne Lagerstatus)."""
    t = get('https://www.mueller.de/search/?q=' + quote('playstation 5 konsole'))
    out = []
    for m in re.finditer(r'"@type":"Product","name":"([^"]+)","url":"(https://www\.mueller\.de/p/[^"]+)"[^{}]*"offers":\{([^{}]*)\}', t):
        title, url, off = html.unescape(m.group(1)), m.group(2), m.group(3)
        pm = re.search(r'"price":([\d.]+)', off)
        if not pm or not is_ps5_with_drive(title):
            continue
        out.append(offer(id=url.rstrip('/').rsplit('-', 1)[-1], source='mueller', shop='Müller', country='DE',
                         title=title, price=float(pm.group(1)), url=url))
    return out


def alternate():
    t = get('https://www.alternate.de/listing.xhtml?q=' + quote('playstation 5 slim'))
    out = []
    for m in re.finditer(r'href="(https://www\.alternate\.de/[^"]+/html/product/(\d+))"[^>]*class="card', t):
        card = txt(t[m.end(): m.end() + 6000].split('class="card ')[0])
        tm = re.search(r'alt="([^"]+)"', t[m.end(): m.end() + 3000])
        pm = re.search(r'€\s*([\d.]+,\d{2})', card)
        title = txt(tm.group(1)) if tm else ''
        if not pm or not is_ps5_with_drive(title) or re.search(r'nicht verfügbar|ausverkauft|nicht lieferbar', card, re.I):
            continue
        out.append(offer(id=m.group(2), source='alternate', shop='Alternate', country='DE', title=title,
                         price=num(pm.group(1)), url=m.group(1)))
    return out


def psdirect():
    """PlayStation Direct (Sonys eigener Shop): Produktseiten -> Produktcodes -> oeffentliche Preis-/Lager-API."""
    hub = get('https://direct.playstation.com/de-de/hardware/ps5')
    pages = sorted(set(re.findall(r'href="(/de-de/buy-consoles/[^"]*playstation5[^"]*)"', hub)))
    pages = [p for p in pages if 'digital' not in p and 'pro-console' not in p]
    codes = {}
    for p in pages:
        m = re.search(r'productHero-component[^>]*data-product-code="(\d+-DE)"', get('https://direct.playstation.com' + p))
        if m:
            codes[m.group(1)] = p
    if not codes:
        raise RuntimeError('keine Produktcodes gefunden')
    r = requests.get('https://api.direct.playstation.com/commercewebservices/ps-direct-de/users/anonymous/products/productList',
                     params={'fields': 'BASIC', 'lang': 'de_DE', 'productCodes': ','.join(codes)},
                     headers={**HEADERS, 'Accept': 'application/json'}, timeout=40)
    r.raise_for_status()
    out = []
    for p in r.json().get('products', []):
        path = codes.get(p.get('code'), '')
        refurb = 'refurbished' in path
        title = txt(p.get('name', '')) + (' (Disc, generalüberholt)' if refurb else '')
        if (p.get('stock') or {}).get('stockLevelStatus') != 'inStock' or not (p.get('price') or {}).get('value'):
            continue
        if not refurb and not is_ps5_with_drive(title + ' Konsole Disc'):
            continue
        out.append(offer(id=p['code'], source='psdirect', shop='PlayStation Direct', country='DE', title=title,
                         price=float(p['price']['value']), url='https://direct.playstation.com' + path,
                         condition='gebraucht' if refurb else 'neu',
                         note='von Sony zertifiziert generalüberholt, mit Garantie' if refurb else 'Sony-Shop'))
    return out


def ricardo():
    """ricardo.ch: nur neue Artikel mit Sofort-kaufen-Preis (gebrauchte = privat, ohne Gewährleistung)."""
    t = get('https://www.ricardo.ch/de/s/' + quote('playstation 5 slim'))
    u = t.replace('\\"', '"')
    out = []
    rec = r'(?:(?!\{"id":")[^\n]){0,1500}?'  # innerhalb eines Artikel-Datensatzes bleiben
    for m in re.finditer(r'\{"id":"(\d+)","title":"([^"]+)"' + rec + r'"conditionKey":"(\w+)"' + rec +
                         r'"buyNowPrice":(null|[\d.]+)(' + rec + r')"productTypeKey"', u):
        aid, title, cond, bn, rest = m.groups()
        if cond != 'new' or bn == 'null' or not is_ps5_with_drive(title):
            continue
        ship = re.search(r'"shipping":\[\{"key":"[^"]+","cost":([\d.]+)', rest)
        out.append(offer(id=aid, source='ricardo', shop='ricardo.ch', country='CH', title=title,
                         price=float(bn) + (float(ship.group(1)) if ship else 0), currency='CHF',
                         url=f'https://www.ricardo.ch/de/a/{aid}/', note='Marktplatz, neu, inkl. Versand'))
    return out


def ebay():
    """Offizielle eBay-Browse-API (nur wenn Zugangsdaten in config.json stehen)."""
    cid, sec = CFG.get('ebay_client_id'), CFG.get('ebay_client_secret')
    if not cid or not sec:
        return None
    tok = requests.post('https://api.ebay.com/identity/v1/oauth2/token', timeout=30,
                        headers={'Authorization': 'Basic ' + base64.b64encode(f'{cid}:{sec}'.encode()).decode(),
                                 'Content-Type': 'application/x-www-form-urlencoded'},
                        data={'grant_type': 'client_credentials', 'scope': 'https://api.ebay.com/oauth/api_scope'})
    tok.raise_for_status()
    token = tok.json()['access_token']
    out = []
    for market, country in (('EBAY_DE', 'DE'), ('EBAY_AT', 'AT'), ('EBAY_CH', 'CH')):
        r = requests.get('https://api.ebay.com/buy/browse/v1/item_summary/search', timeout=40,
                         headers={'Authorization': f'Bearer {token}', 'X-EBAY-C-MARKETPLACE-ID': market},
                         params={'q': 'playstation 5 slim disc konsole', 'limit': 100, 'sort': 'price',
                                 'filter': 'buyingOptions:{FIXED_PRICE},conditionIds:{1000|1500|2000|2500}'})
        r.raise_for_status()
        for it in r.json().get('itemSummaries', []):
            title = it.get('title', '')
            if not is_ps5_with_drive(title):
                continue
            p = it.get('price') or {}
            ship = ((it.get('shippingOptions') or [{}])[0].get('shippingCost') or {}).get('value', 0)
            new = it.get('conditionId') == '1000'
            if not new and 'refurbished' not in (it.get('condition') or '').lower():
                continue  # gebraucht nur als Refurbished (mit Gewährleistung)
            out.append(offer(id=it['itemId'], source='ebay', shop=f"eBay ({(it.get('seller') or {}).get('username', '?')})",
                             country=country, title=title, price=float(p.get('value', 0)) + float(ship or 0),
                             currency=p.get('currency', 'EUR'), url=it.get('itemWebUrl'),
                             condition='neu' if new else 'gebraucht', note='' if new else 'eBay Refurbished'))
    return out


GS_STATE = {}  # wird von run() mit dem gespeicherten Zaehlerstand belegt


class Skip(Exception):
    """Quelle wird in diesem Lauf bewusst ausgelassen; letzte Angebote bleiben stehen."""


def google_shopping():
    """Google Shopping ueber SerpApi (offizieller Dienst, kein direkter Google-Abruf).
    Pro Lauf nur 1 Suche, rotierend DE -> AT -> CH, mit Monatsbudget."""
    key = CFG.get('serpapi_key')
    if not key:
        return None
    month = datetime.now().strftime('%Y-%m')
    if GS_STATE.get('month') != month:
        GS_STATE.update(month=month, used=0)
    if GS_STATE['used'] >= CFG.get('serpapi_monthly_limit', 90):
        raise RuntimeError(f"Monatsbudget von {CFG.get('serpapi_monthly_limit', 90)} Suchen aufgebraucht")
    GS_STATE['runs'] = GS_STATE.get('runs', 0) + 1
    if GS_STATE['runs'] % CFG.get('serpapi_every_n_runs', 1):
        raise Skip(f"nur bei jedem {CFG['serpapi_every_n_runs']}. Lauf (Suchkontingent)")
    country = ('DE', 'AT', 'CH')[GS_STATE.get('turn', 0) % 3]
    GS_STATE['turn'] = GS_STATE.get('turn', 0) + 1
    GS_STATE['used'] += 1
    r = requests.get('https://serpapi.com/search.json', timeout=60, params={
        'engine': 'google_shopping', 'q': 'PS5 Slim Disc', 'gl': country.lower(),
        'hl': 'de', 'google_domain': {'DE': 'google.de', 'AT': 'google.at', 'CH': 'google.ch'}[country], 'api_key': key})
    r.raise_for_status()
    j = r.json()
    if j.get('error') and 'any results' not in j['error']:
        raise RuntimeError(j['error'])
    out = []
    for it in j.get('shopping_results', []):
        title = it.get('title', '')
        price = it.get('extracted_price')
        if not price or not is_ps5_with_drive(title):
            continue
        used = bool(it.get('second_hand_condition')) or USED.search(title)
        out.append(offer(id=it.get('product_id') or it.get('link') or title, source='google', country=country,
                         shop=it.get('source') or 'Google Shopping', title=title, price=float(price),
                         currency='CHF' if country == 'CH' else 'EUR',
                         url=it.get('link') or it.get('product_link') or '',
                         condition='gebraucht' if used else 'neu',
                         note='über Google Shopping' + (f" · {it['second_hand_condition']}" if it.get('second_hand_condition') else '')))
    return out


SOURCES = {
    'mydealz': ('mydealz.de', 'DE', mydealz),
    'preisjaeger': ('preisjaeger.at', 'AT', preisjaeger),
    'preispirat': ('preispirat.ch', 'CH', preispirat),
    'amazon': ('Amazon.de', 'DE', amazon),
    'mediamarkt': ('MediaMarkt', 'DE', mediamarkt),
    'saturn': ('Saturn', 'DE', saturn),
    'otto': ('OTTO', 'DE', otto),
    'billiger': ('billiger.de', 'DE', billiger),
    'mueller': ('Müller', 'DE', mueller),
    'alternate': ('Alternate', 'DE', alternate),
    'psdirect': ('PlayStation Direct', 'DE', psdirect),
    'rebuy': ('rebuy', 'DE', rebuy),
    'ricardo': ('ricardo.ch', 'CH', ricardo),
    'ebay': ('eBay DE/AT/CH', 'DACH', ebay),
    'google': ('Google Shopping', 'DACH', google_shopping),
}

# Offizielle / etablierte Haendler im DACH-Raum. Nur diese loesen Alarme aus (alert_only_trusted).
TRUSTED = re.compile(
    r'^(amazon(\.de)?|media ?markt|saturn|otto|expert|euronics|medimax|cyberport|alternate|notebooksbilliger|'
    r'galaxus|digitec|conrad|m[üu]ller|gamestop|smyths|toys ?r ?us|lidl|aldi|marktkauf|real|'
    r'galeria|coolblue|proshop|mindfactory|playstation direct|sony|direct\.playstation|rebuy|'
    r'libro|hartlauer|e-tec|electronic4you|interspar|brack|interdiscount|microspot|fust|manor|melectronics|'
    r'mediamarkt\.(at|ch)|amazon\.(fr|it|es|nl))\b', re.I)


WATCH_EMPTY = ('amazon', 'mediamarkt', 'saturn', 'otto', 'billiger', 'mueller', 'alternate')  # liefern sonst immer Treffer
FEEDS = ('mydealz', 'preisjaeger', 'preispirat')  # leichte RSS-Feeds: stuendlich (--feeds)
if IN_CLOUD:
    FEEDS += ('amazon',)  # in der Cloud nur das Lesen der PC-Daten (ntfy) - kostet nichts, daher auch stuendlich
# Beim Zusammenfassen gleicher Angebote gewinnt die direktere Quelle
PRIORITY = {'billiger': 1, 'google': 2, 'ricardo': 2, 'ebay': 2, 'mydealz': 3, 'preisjaeger': 3, 'preispirat': 3}


def is_trusted(o):
    if o['source_key'] in ('ricardo', 'ebay'):
        return False  # Marktplatz: Haendler unbekannt
    if o['source_key'] in ('mediamarkt', 'saturn'):
        return '(' not in o['shop']  # "(Verkaeufer)" = Marktplatz-Haendler
    shop = (o['shop'] or '').strip()
    if re.search(r'seller|marketplace|marktplatz', shop, re.I):
        return False  # z. B. "Amazon.de - Amazon.de-Seller" = Drittanbieter
    return bool(TRUSTED.search(shop))


# ---------------------------------------------------------------- Ablauf

def chf_per_eur():
    try:
        t = requests.get('https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml', timeout=20).text
        return float(re.search(r"currency='CHF' rate='([\d.]+)'", t).group(1))
    except Exception:
        return CFG['chf_per_eur_fallback']


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    return {'offers': [], 'seen': {}, 'alerts': [], 'sources': {}, 'lowest': None, 'last_run': None}


def notify(title, text, url):
    if NO_NOTIFY:
        return
    if CFG.get('ntfy_topic'):
        # Handy-Push ueber ntfy.sh (kostenlos); JSON-Variante, damit Umlaute/€ im Titel funktionieren
        try:
            requests.post('https://ntfy.sh/', timeout=20, json={
                'topic': CFG['ntfy_topic'], 'title': title, 'message': text, 'click': url,
                'tags': ['video_game'], 'priority': 4}).raise_for_status()
        except Exception as e:
            log(f'ntfy-Push fehlgeschlagen: {e}')
    if os.name == 'nt' and not IN_CLOUD:
        try:
            subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(ROOT / 'notify.ps1'),
                            '-Title', title, '-Text', text, '-Url', url], timeout=30,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except Exception as e:
            log(f'Windows-Meldung fehlgeschlagen: {e}')


def norm_shop(s):
    s = re.sub(r'\(.*?\)', '', (s or '').lower())
    s = re.sub(r'\.(de|at|ch|com|net|eu)\b', '', s)
    s = re.sub(r'\b(versand|online|shop|gmbh|deutschland)\b', '', s)
    return re.sub(r'[^a-z0-9äöü]', '', s)


def dedupe(offers):
    """Gleicher Haendler + gleiches Land + gleicher Preis aus verschiedenen Quellen -> nur die direkteste Quelle behalten."""
    best = {}
    for o in offers:
        k = (norm_shop(o['shop']), o['country'], round(o['price_eur']))
        cur = best.get(k)
        if cur is None:
            best[k] = o
        elif cur['source_key'] != o['source_key'] and PRIORITY.get(o['source_key'], 0) < PRIORITY.get(cur['source_key'], 0):
            best[k] = o
    keep = {id(o) for o in best.values()}
    # gleiche Quelle darf mehrere Produkte zum gleichen Preis haben
    return [o for o in offers if id(o) in keep or
            best[(norm_shop(o['shop']), o['country'], round(o['price_eur']))]['source_key'] == o['source_key']]


def run(keys=None):
    state = load_state()
    rate = chf_per_eur()
    GS_STATE.update(state.get('google_shopping') or {})
    keys = list(keys or SOURCES)
    results = {}

    def one(key):
        try:
            res = SOURCES[key][2]()
            return key, res, None
        except Skip as e:
            return key, None, Skip(str(e))
        except Exception as e:
            # Zugangsdaten aus Fehlermeldungen entfernen (Actions-Logs sind oeffentlich)
            msg = re.sub(r'(api_key|token|secret|client_id)=[^&\s]+', r'\1=***', str(e), flags=re.I)
            return key, None, f'{type(e).__name__}: {msg[:120]}'

    with ThreadPoolExecutor(6) as ex:
        for key, res, err in ex.map(one, keys):
            results[key] = (res, err)

    old_by_source = {}
    for o in state['offers']:
        old_by_source.setdefault(o['source_key'], []).append(o)

    offers = []
    now = now_iso()
    for key in SOURCES:
        if key not in keys:  # in diesem Lauf nicht abgefragt (z. B. Feed-Modus): alte Angebote behalten
            offers.extend(old_by_source.get(key, []))
    for key, (res, err) in results.items():
        name, country, _ = SOURCES[key]
        if res is None and err is None:
            state['sources'][key] = {'name': name, 'status': 'aus', 'msg': 'kein API-Zugang eingetragen', 'count': 0}
            continue
        if isinstance(err, Skip):
            kept = old_by_source.get(key, [])
            offers.extend(kept)
            state['sources'][key] = {**state['sources'].get(key, {}), 'name': name, 'status': 'ok', 'msg': str(err), 'count': len(kept)}
            log(f'{key}: ausgelassen ({err})')
            continue
        if key == 'google' and not err:
            # Google Shopping fragt pro Lauf nur ein Land ab: Treffer der anderen Laender behalten
            done = {o['country'] for o in res} or {('DE', 'AT', 'CH')[(GS_STATE['turn'] - 1) % 3]}
            offers.extend(o for o in old_by_source.get(key, []) if o['country'] not in done)
        if err:
            # Quelle gerade nicht erreichbar: letzte bekannte Angebote behalten
            # (ausser Amazon in der Cloud: ohne frische PC-Daten waeren die Preise veraltet)
            kept = [] if key == 'amazon' and IN_CLOUD else old_by_source.get(key, [])
            offers.extend(kept)
            state['sources'][key] = {'name': name, 'status': 'fehler', 'msg': err, 'count': len(kept), 'at': now}
            log(f'{key}: FEHLER {err}')
            continue
        uniq = {}
        for o in res:
            # Preisuntergrenze: darunter sind es Spiele, Zubehoer oder Lockangebote, keine Konsole
            if not o.get('price') or o['price'] < (250 if o['condition'] == 'gebraucht' else 300):
                continue
            o['source_key'] = key
            o['key'] = f"{key}|{o['id']}"
            o['price_eur'] = round(o['price'] / rate, 2) if o['currency'] == 'CHF' else o['price']
            o['trusted'] = is_trusted(o)
            if not o['trusted'] and o['condition'] == 'neu' and o['price_eur'] < CFG.get('suspicious_below_eur', 450):
                o['note'] = ('⚠ Preis auffällig niedrig – möglicher Fake-Shop. ' + o['note']).strip()
            o['local'] = bool(re.search(r'\blokal', o['title'] + ' ' + o['note'], re.I))
            if o['key'] not in uniq or o['price_eur'] < uniq[o['key']]['price_eur']:
                uniq[o['key']] = o
        offers.extend(uniq.values())
        prev = state['sources'].get(key, {})
        if not uniq and key in WATCH_EMPTY and prev.get('count'):
            # Shop lieferte sonst Treffer, jetzt keine: vermutlich Seite umgebaut -> sichtbar machen
            state['sources'][key] = {'name': name, 'status': 'fehler', 'count': 0, 'at': now,
                                     'msg': '0 Treffer – Seite evtl. umgebaut, Parser prüfen'}
            log(f'{key}: WARNUNG 0 Treffer')
            continue
        state['sources'][key] = {'name': name, 'status': 'ok', 'msg': '', 'count': len(uniq), 'at': now}
        log(f'{key}: {len(uniq)} PS5-Angebote')

    for o in offers:  # aeltere gespeicherte Angebote ohne diese Felder
        o.setdefault('trusted', is_trusted(o))
        o.setdefault('local', bool(re.search(r'\blokal', o['title'] + ' ' + o['note'], re.I)))
    before = len(offers)
    offers = dedupe(offers)
    if before != len(offers):
        log(f'{before - len(offers)} doppelte Angebote zusammengefasst')

    # Relevanz: neu bis show_max, gebraucht nur bis used_max
    relevant = [o for o in offers
                if (o['condition'] == 'neu' and o['price_eur'] <= CFG['show_max_eur'])
                or (o['condition'] == 'gebraucht' and o['price_eur'] <= CFG['used_max_eur'])]
    # abgelaufene Deals nur noch als Orientierung (eigener Bereich, kein Alarm)
    expired = sorted((o for o in relevant if o.get('expired')), key=lambda o: o.get('posted', ''), reverse=True)
    shown = [o for o in relevant if not o.get('expired')]

    def fresh(o):  # Deal mit unbekanntem Status: nur die ersten 3 Tage alarmwuerdig
        return o.get('kind') != 'deal' or o.get('expired') is False or \
            datetime.now(timezone.utc) - datetime.fromisoformat(o['posted']) < timedelta(days=3)

    new_alerts = []
    for o in shown:
        seen = state['seen'].get(o['key'])
        o['first_seen'] = seen['first_seen'] if seen else now
        o['is_new'] = not seen
        if seen:
            seen['min_eur'] = min(seen['min_eur'], o['price_eur'])
        else:
            state['seen'][o['key']] = {'first_seen': now, 'min_eur': o['price_eur']}
        alerted = state['seen'][o['key']].get('alerted_eur')
        wanted = (o['trusted'] or not CFG.get('alert_only_trusted', True)) and (not o['local'] or CFG.get('alert_local', False)) and fresh(o)
        # Deals (mydealz & Co.) von offiziellen Haendlern melden schon unter deal_alert_below_eur (z. B. OTTO + Gutschein)
        limit = max(CFG['alert_below_eur'], CFG.get('deal_alert_below_eur', 0)) if o.get('kind') == 'deal' else CFG['alert_below_eur']
        if wanted and o['price_eur'] < limit and (alerted is None or o['price_eur'] < alerted - 5):
            state['seen'][o['key']]['alerted_eur'] = o['price_eur']
            new_alerts.append(o)
    shown.sort(key=lambda o: o['price_eur'])

    good = [o for o in shown if o['trusted'] and not o['local'] and o['country'] != 'CH']  # CH ohne Zoll nicht vergleichbar
    if good and (not state['lowest'] or good[0]['price_eur'] < state['lowest']['price_eur']):
        b = good[0]
        state['lowest'] = {k: b[k] for k in ('title', 'shop', 'country', 'price_eur', 'url', 'condition')} | {'at': now}

    for o in new_alerts:
        state['alerts'].append({'at': now, 'claude': False,
                                **{k: o[k] for k in ('key', 'title', 'shop', 'country', 'price_eur', 'price', 'currency', 'url', 'condition', 'note')}})
        cur = '' if o['currency'] == 'EUR' else f" ({o['price']:.0f} {o['currency']})"
        tip = 'Deal-Tipp: ' if o['price_eur'] >= CFG['alert_below_eur'] else ''
        notify(f"{tip}PS5 für {o['price_eur']:.0f} €{cur} – {o['shop']}",
               f"{o['title'][:110]}\n{o['country']} · {o['condition']}{' · ' + o['note'] if o['note'] else ''}", o['url'])
    state['alerts'] = state['alerts'][-200:]
    state['offers'] = offers
    state['shown'] = shown
    state['expired'] = expired
    if set(keys) == set(SOURCES):
        state['last_run'] = now
    state['last_feed_run'] = now
    state['rate_chf_per_eur'] = rate
    state['google_shopping'] = dict(GS_STATE)
    state['config'] = {k: CFG[k] for k in ('alert_below_eur', 'show_max_eur', 'used_max_eur', 'until')}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')
    public = {k: state.get(k) for k in ('shown', 'expired', 'lowest', 'sources', 'last_run', 'last_feed_run', 'rate_chf_per_eur', 'config')}
    public['alerts'] = state['alerts'][-20:]
    DATA_JS.write_text('window.PS5 = ' + json.dumps(public, ensure_ascii=False) + ';\n', encoding='utf-8')
    log(f'fertig: {len(shown)} relevante Angebote, {len(new_alerts)} neue Alarme')
    return new_alerts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--if-stale', action='store_true')
    ap.add_argument('--claude', action='store_true')
    ap.add_argument('--no-notify', action='store_true')
    ap.add_argument('--feeds', action='store_true', help='nur die Deal-Feeds abrufen (stuendlich)')
    ap.add_argument('--amazon-relay', action='store_true', help='PC: Amazon abfragen und an die Cloud weiterreichen')
    a = ap.parse_args()
    if a.amazon_relay:
        if datetime.now().date().isoformat() <= CFG['until']:
            amazon_relay_send()
        return
    if datetime.now().date().isoformat() > CFG['until']:
        log('Suchzeitraum vorbei – nichts zu tun.')
        if a.claude:
            print(json.dumps({'expired': True, 'alerts': []}))
        return
    with run_lock():
        state = load_state()
        last = state.get('last_run')
        stale = not last or datetime.now(timezone.utc) - datetime.fromisoformat(last) > timedelta(hours=CFG['stale_hours'])
        if a.feeds:
            run(FEEDS if not stale else None)  # ist der volle Lauf ueberfaellig (PC war aus), gleich alles abrufen
        elif stale or not (a.if_stale or a.claude):
            run()
        if a.claude:
            claude_report()


LOCK = ROOT / 'ps5.lock'


class run_lock:
    """Verhindert, dass stuendlicher Feed-Lauf, 8-Std.-Lauf und Claude-Task gleichzeitig ps5.json schreiben."""
    def __enter__(self):
        for _ in range(120):
            try:
                self.fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                if time.time() - LOCK.stat().st_mtime > 900:  # haengengebliebene Sperre (> 15 Min.) aufheben
                    LOCK.unlink(missing_ok=True)
                else:
                    time.sleep(5)
        raise RuntimeError('ps5.lock blockiert')

    def __exit__(self, *exc):
        os.close(self.fd)
        LOCK.unlink(missing_ok=True)


def claude_report():
    """Gibt die noch nicht per Claude gemeldeten Alarme aus und markiert sie als gemeldet."""
    state = load_state()
    pending = [x for x in state['alerts'] if not x.get('claude')]
    for x in state['alerts']:
        x['claude'] = True
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    print('CLAUDE_ALERTS ' + json.dumps({'expired': False, 'last_run': state['last_run'], 'alerts': pending}, ensure_ascii=False))


if __name__ == '__main__':
    main()
