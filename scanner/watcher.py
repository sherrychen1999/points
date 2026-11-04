# -*- coding: utf-8 -*-
"""
三日中碳完賽禮 置頂文留言掃描器（公司電腦用，Windows / Mac 皆可）
每 INTERVAL_SEC 秒：開置頂文 → 展開留言 → 找「#R 開頭 6 碼」完賽碼 → 送到 Apps Script → 後端配序號並 LINE 推播

第一次執行會開一個瀏覽器視窗讓你登入 Facebook（用 a0937 金鶯診所帳號），登入一次就記住。
設定在 config.json。關掉視窗就停。
"""
import json, os, re, sys, time, urllib.parse, urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, 'config.json')
PROFILE = os.path.join(HERE, 'fb_profile')
STATE_PATH = os.path.join(HERE, 'state.json')
CODE_RE = re.compile(r'#\s*(R[A-Z2-9]{5})(?![A-Z0-9])', re.I)

def now(): return datetime.now().strftime('%H:%M:%S')
def log(msg): print(f'[{now()}] {msg}', flush=True)

def load_cfg():
    with open(CFG_PATH, encoding='utf-8') as f: cfg = json.load(f)
    for k in ('POST_URL', 'EXEC_URL', 'API_KEY'):
        if not cfg.get(k) or '請填' in str(cfg.get(k)):
            log(f'config.json 的 {k} 還沒填，請先填好再啟動。'); input('按 Enter 離開'); sys.exit(1)
    cfg.setdefault('INTERVAL_SEC', 300); cfg.setdefault('EXPAND_ROUNDS', 6)
    return cfg

def load_state():
    try:
        with open(STATE_PATH, encoding='utf-8') as f: return json.load(f)
    except Exception: return {'sent': {}, 'recheck': [], 'rechecked': {}}
def save_state(st):
    with open(STATE_PATH, 'w', encoding='utf-8') as f: json.dump(st, f, ensure_ascii=False, indent=1)

def send(cfg, action, comments):
    q = urllib.parse.urlencode({'key': cfg['API_KEY'], 'action': action, 'comments': comments})
    url = cfg['EXEC_URL'] + ('&' if '?' in cfg['EXEC_URL'] else '?') + q
    req = urllib.request.Request(url, headers={'User-Agent': 'relay-watcher'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode('utf-8', 'ignore')

EXTRACT_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const codeRe = /#\s*(R[A-Z2-9]{5})(?![A-Z0-9])/i;
  const nodes = Array.from(document.querySelectorAll('div[role="article"]'));
  for (const n of nodes) {
    const txt = n.innerText || '';
    const m = txt.match(codeRe);
    if (!m) continue;
    const code = m[1].toUpperCase();
    if (seen.has(code)) continue;
    // 作者：第一個指向個人檔案的連結
    let id = '', name = '';
    const links = Array.from(n.querySelectorAll('a[href]'));
    for (const a of links) {
      const h = a.getAttribute('href') || '';
      let mm = h.match(/\/user\/(\d+)/) || h.match(/profile\.php\?id=(\d+)/) || h.match(/facebook\.com\/(\d{6,})/);
      if (mm) { id = mm[1]; name = (a.innerText || '').trim(); if (name) break; }
    }
    if (!name) { const a = links.find(a => (a.innerText || '').trim()); name = a ? a.innerText.trim() : ''; }
    seen.add(code);
    out.push({ code, id, name });
  }
  return out;
}
"""

EXPAND_JS = r"""
() => {
  let n = 0;
  const re = /^(查看\s*\d+\s*則回覆|查看更多留言|更多留言|顯示更多留言|View more comments|View \d+ (more )?repl(y|ies))$/;
  for (const b of document.querySelectorAll('[role="button"],span,div')) {
    const t = (b.innerText || '').trim();
    if (re.test(t) && b.offsetParent !== null) { try { b.click(); n++; } catch (e) {} }
  }
  return n;
}
"""

SORT_JS = r"""
() => {
  // 把留言排序切到「所有留言」
  const btn = Array.from(document.querySelectorAll('[role="button"]')).find(b => /^(最相關|最新|所有留言|Most relevant|Newest|All comments)$/.test((b.innerText||'').trim()));
  if (!btn) return 'no-sort-btn';
  btn.click();
  return 'clicked';
}
"""
SORT_PICK_JS = r"""
() => {
  const it = Array.from(document.querySelectorAll('[role="menuitem"],[role="menuitemradio"]')).find(b => /所有留言|All comments/.test(b.innerText||''));
  if (!it) return 'no-item';
  it.click(); return 'picked';
}
"""


SEE_MORE_JS = r"""
() => {
  // 展開貼文內文的「查看更多」，標籤常在最後被折起來
  let n = 0;
  for (const b of document.querySelectorAll('div[role="article"] [role="button"]')) {
    const t = (b.innerText || '').trim();
    if ((t === '查看更多' || t === 'See more') && b.offsetParent !== null) { try { b.click(); n++; } catch (e) {} }
  }
  return n;
}
"""

POSTDATES_JS = r"""
(tag) => {
  // 社團個人頁：每篇貼文(role=article，不含留言)取時間連結文字＋是否帶標籤
  const out = [];
  const tagRe = tag ? new RegExp('[#＃]\\s*' + tag) : null;
  for (const art of document.querySelectorAll('div[role="article"]')) {
    if (art.parentElement && art.parentElement.closest('div[role="article"]')) continue; // 留言
    const a = art.querySelector('a[href*="/posts/"], a[href*="/permalink/"]');
    if (!a) continue;
    const t = (a.getAttribute('aria-label') || a.innerText || '').trim();
    if (!t) continue;
    const txt = art.innerText || '';
    const hrefTag = tag && Array.from(art.querySelectorAll('a[href*="/hashtag/"]')).some(h => decodeURIComponent(h.getAttribute('href') || '').indexOf('/hashtag/' + tag) >= 0);
    out.push({ t: t, tagged: !tagRe || tagRe.test(txt) || !!hrefTag });
  }
  return out;
}
"""

def parse_fb_time(t, now):
    """把 FB 的相對時間（3 小時前／昨天／2 天前／10月28日／2026年10月28日）換成 yyyy-mm-dd，看不懂回 None"""
    import re as _re
    from datetime import timedelta
    t = t.replace(' ', '')
    if t.startswith('剛剛'):
        return now.strftime('%Y-%m-%d')
    m = _re.match(r'(\d+)分鐘', t)
    if m: return (now - timedelta(minutes=int(m.group(1)))).strftime('%Y-%m-%d')
    m = _re.match(r'(\d+)小時', t)
    if m: return (now - timedelta(hours=int(m.group(1)))).strftime('%Y-%m-%d')
    if t.startswith('昨天'):
        return (now - timedelta(days=1)).strftime('%Y-%m-%d')
    m = _re.match(r'(\d+)天前', t)
    if m: return (now - timedelta(days=int(m.group(1)))).strftime('%Y-%m-%d')
    m = _re.match(r'(\d{4})年(\d{1,2})月(\d{1,2})日', t)
    if m: return f'{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'
    m = _re.match(r'(\d{1,2})月(\d{1,2})日', t)
    if m:
        y = now.year
        d = f'{y:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}'
        if d > now.strftime('%Y-%m-%d'): d = f'{y-1:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}'
        return d
    return None

def member_post_dates(page, cfg, fb_id):
    """開社團個人頁，捲幾下，回傳這個人最近的發文日期（去重、排序）"""
    m = re.search(r'/groups/(\d+)', cfg['POST_URL'])
    if not m or not fb_id: return []
    url = f"https://www.facebook.com/groups/{m.group(1)}/user/{fb_id}/"
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=60000)
        page.wait_for_timeout(3500)
        for _ in range(int(cfg.get('PROFILE_SCROLLS', 3))):
            page.mouse.wheel(0, 2500); page.wait_for_timeout(1500)
        try:
            if page.evaluate(SEE_MORE_JS): page.wait_for_timeout(1200)
        except Exception: pass
        raw = page.evaluate(POSTDATES_JS, cfg.get('TAG', '中碳日'))
    except Exception as e:
        log(f'讀 {fb_id} 的發文頁失敗：{e!r}'); return []
    now = datetime.now()
    ds = sorted({d for d in (parse_fb_time(x['t'], now) for x in raw if x['tagged']) if d})
    skipped = sum(1 for x in raw if not x['tagged'])
    log(f'  {fb_id} 帶標籤的發文日期：{",".join(ds) if ds else "（沒有）"}；沒帶標籤 {skipped} 篇')
    return ds

def scan_once(page, cfg):
    page.goto(cfg['POST_URL'], wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(4000)
    if 'login' in page.url or page.locator('input[name="email"]').count() > 0:
        return None  # 未登入
    try:
        r = page.evaluate(SORT_JS)
        if r == 'clicked':
            page.wait_for_timeout(800); page.evaluate(SORT_PICK_JS); page.wait_for_timeout(1500)
    except Exception: pass
    for _ in range(int(cfg['EXPAND_ROUNDS'])):
        try:
            n = page.evaluate(EXPAND_JS)
        except Exception: n = 0
        if not n: break
        page.wait_for_timeout(2000)
    return page.evaluate(EXTRACT_JS)

def recheck_due(page, cfg, st):
    """核對中（還沒滿連續 3 天）的人：每 RECHECK_MIN 分鐘重讀一次發文，滿了後端就發券"""
    items = st.get('recheck') or []
    if not items: return
    last = st.setdefault('rechecked', {})
    gap = int(cfg.get('RECHECK_MIN', 60)) * 60
    due = [x for x in items if time.time() - last.get(x.split('｜')[0], 0) >= gap][:int(cfg.get('RECHECK_MAX', 10))]
    if not due: return
    when = datetime.now().strftime('%Y-%m-%d %H:%M')
    lines = []
    for x in due:
        f = x.split('｜')
        ds = member_post_dates(page, cfg, f[1]) if len(f) > 1 and f[1] else []
        lines.append(f"{f[0]}｜{f[1] if len(f) > 1 else ''}｜{f[2] if len(f) > 2 else ''}｜{when}｜{','.join(ds)}")
        last[f[0]] = time.time()
    res = send(cfg, 'relay_comment', ';;'.join(lines))
    log(f'重新核對 {len(due)} 人 → 後端：{res[:200]}')
    try:
        j = json.loads(res)
        if j.get('ok'): st['recheck'] = j.get('recheck', [])
    except Exception: pass
    save_state(st)

# ---------- 每小時看一次社團最新貼文，記下 #中碳日（儀表板當天就有參與人數） ----------
FEED_HOVER_JS = r"""
() => {
  // FB 的貼文時間連結要滑過才會有網址：對每篇貼文裡還沒網址的連結送滑鼠事件
  let n = 0;
  for (const post of document.querySelectorAll('div[role="feed"] > div')) {
    if (!(post.innerText || '').trim()) continue;
    for (const a of post.querySelectorAll('a[role="link"]')) {
      const h = a.getAttribute('href') || '';
      if (h === '#' || h === '' ) {
        for (const ev of ['mouseover', 'mouseenter', 'focus']) { try { a.dispatchEvent(new MouseEvent(ev, { bubbles: true })); } catch (e) {} }
        n++;
      }
    }
  }
  return n;
}
"""
FEED_JS = r"""
(args) => {
  const tag = args.tag, gid = args.gid;
  const tagRe = new RegExp('[#＃]\\s*' + tag);
  const out = [];
  for (const post of document.querySelectorAll('div[role="feed"] > div')) {
    const txt = post.innerText || '';
    if (!txt.trim()) continue;
    let pid = '', tlab = '', fb = '', name = '';
    for (const a of post.querySelectorAll('a[href]')) {
      const h = a.getAttribute('href') || '';
      let m = h.match(/\/groups\/[^/?]+\/(?:posts|permalink)\/(\d+)/);
      if (m && h.indexOf('comment_id') < 0) { pid = m[1]; tlab = (a.getAttribute('aria-label') || a.innerText || '').trim(); break; }
    }
    if (!pid) {
      for (const a of post.querySelectorAll('a[href]')) {
        const m = (a.getAttribute('href') || '').match(/set=pcb\.(\d+)/);
        if (m) { pid = m[1]; break; }
      }
    }
    for (const a of post.querySelectorAll('a[href]')) {
      const m = (a.getAttribute('href') || '').match(/\/groups\/[^/?]+\/user\/(\d+)/);
      if (m) { fb = m[1]; name = (a.innerText || '').trim(); if (name) break; }
    }
    const head = txt.slice(0, 160);
    const hrefTag = Array.from(post.querySelectorAll('a[href*="/hashtag/"]')).some(h => decodeURIComponent(h.getAttribute('href') || '').indexOf('/hashtag/' + tag) >= 0);
    out.push({ pid: pid, fb: fb, name: name, t: tlab, admin: /管理員|版主|Admin|Moderator/.test(head),
               anon: /匿名成員|Anonymous member/.test(head), tagged: tagRe.test(txt) || hrefTag, head: head.replace(/\s+/g, ' ').slice(0, 60) });
  }
  return out;
}
"""

def feed_due(cfg, st):
    """該不該掃社團：凌晨 1–7 點不掃；兩次之間隔 FEED_MIN±10 分鐘（隨機）；被 FB 擋過就不掃"""
    import random
    if st.get('feed_blocked'): return False
    h = datetime.now().hour
    if int(cfg.get('FEED_QUIET_FROM', 1)) <= h < int(cfg.get('FEED_QUIET_TO', 7)): return False
    return time.time() >= st.get('feed_next', 0)

def feed_scan(page, cfg, st, test=False):
    """開社團（新貼文排序）捲幾下，把 #中碳日 貼文送後端（action=relay_tag_scan）"""
    import random
    m = re.search(r'/groups/(\d+)', cfg['POST_URL'])
    if not m: return
    gid = m.group(1)
    st['feed_next'] = time.time() + 60 * (int(cfg.get('FEED_MIN', 60)) + random.randint(-10, 10))
    page.goto(f'https://www.facebook.com/groups/{gid}/?sorting_setting=CHRONOLOGICAL', wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(5000)
    if 'checkpoint' in page.url or 'login' in page.url or page.locator('input[name="email"]').count() > 0:
        st['feed_blocked'] = datetime.now().strftime('%Y-%m-%d %H:%M'); save_state(st)
        log('掃社團時 FB 要求驗證／登入，先停止掃社團（置頂文照常）。處理好後刪掉 state.json 裡的 feed_blocked 再重啟。')
        return
    seen = {}
    for i in range(int(cfg.get('FEED_SCROLLS', 8))):
        try:
            page.evaluate(FEED_HOVER_JS); page.wait_for_timeout(1200)
            try:
                if page.evaluate(SEE_MORE_JS): page.wait_for_timeout(800)
            except Exception: pass
            for x in page.evaluate(FEED_JS, {'tag': cfg.get('TAG', '中碳日'), 'gid': gid}):
                k = x['pid'] or x['head']
                if k not in seen or (x['pid'] and not seen[k].get('pid')): seen[k] = x
        except Exception as e:
            log(f'讀社團貼文出錯：{e!r}')
        page.mouse.wheel(0, 1800); page.wait_for_timeout(1800 + random.randint(0, 1200))
    now = datetime.now(); today = now.strftime('%Y-%m-%d')
    first = not st.get('feed_init')
    done = st.setdefault('feed_sent', {})
    rows, skipped = [], 0
    for x in seen.values():
        if test:
            log(f"  [{'#中碳日' if x['tagged'] else '      '}] pid={x['pid'] or '?'} fb={x['fb'] or '?'} {x['name'][:10]} 時間='{x['t'][:16]}' 管理員={x['admin']} 匿名={x['anon']} | {x['head'][:40]}")
        if not x['tagged'] or not x['pid'] or not x['fb'] or x['admin'] or x['anon'] or x['pid'] in done: continue
        d = parse_fb_time(x['t'], now) if x['t'] else None
        if not d:
            if first: skipped += 1; continue   # 第一次掃、看不出日期的舊貼文交給早上的排程
            d = today
        rows.append(f"{d}｜{x['fb']}｜{x['name'].replace('｜', ' ').replace(';', ' ')}｜{x['pid']}")
    log(f'社團最新貼文 {len(seen)} 篇，其中 #中碳日 新的 {len(rows)} 篇' + (f'（看不出日期略過 {skipped} 篇）' if skipped else ''))
    if test:
        for r in rows: log('  會送：' + r)
        return
    if rows:
        res = send_rows(cfg, 'relay_tag_scan', ';;'.join(rows))
        log(f'  → 後端：{res[:160]}')
        try:
            if json.loads(res).get('ok'):
                for r in rows: done[r.split('｜')[3]] = today
        except Exception: pass
    st['feed_init'] = today
    # 只留最近 7 天的紀錄
    cut = (now.timestamp() - 7 * 86400)
    for k in [k for k, v in done.items() if datetime.strptime(v, '%Y-%m-%d').timestamp() < cut]: done.pop(k, None)
    save_state(st)

def send_rows(cfg, action, rows):
    q = urllib.parse.urlencode({'key': cfg['API_KEY'], 'action': action, 'rows': rows})
    url = cfg['EXEC_URL'] + ('&' if '?' in cfg['EXEC_URL'] else '?') + q
    req = urllib.request.Request(url, headers={'User-Agent': 'relay-watcher'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode('utf-8', 'ignore')

def wait_login(page):
    log('請在開啟的瀏覽器視窗登入 Facebook（a0937 金鶯診所帳號）。登入完成後不用做別的事，程式會自己繼續。')
    page.goto('https://www.facebook.com/', wait_until='domcontentloaded')
    for _ in range(120):  # 最多等 20 分鐘
        page.wait_for_timeout(10000)
        try:
            if page.locator('input[name="email"]').count() == 0 and 'login' not in page.url:
                log('偵測到已登入。'); return True
        except Exception: pass
    return False

def main():
    cfg = load_cfg(); st = load_state()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE, headless=False, locale='zh-TW',
                                                   viewport={'width': 1100, 'height': 900}, args=['--disable-blink-features=AutomationControlled'])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if '--feedtest' in sys.argv:
            log('測試：掃一次社團最新貼文，只印結果、不送出。')
            feed_scan(page, cfg, st, test=True); input('按 Enter 離開'); return
        if '--login' in sys.argv:
            wait_login(page); log('登入完成，可以關閉視窗，之後雙擊「啟動.bat」開始掃描。'); input('按 Enter 離開'); return
        log(f'開始掃描，每 {cfg["INTERVAL_SEC"]} 秒一次。關掉這個視窗就停止。')
        while True:
            try:
                found = scan_once(page, cfg)
                if found is None:
                    if not wait_login(page): log('登入逾時，10 分鐘後再試'); time.sleep(600); continue
                    found = scan_once(page, cfg) or []
                new = [c for c in found if c['code'] not in st['sent']]
                if new:
                    when = datetime.now().strftime('%Y-%m-%d %H:%M')
                    for c in new:
                        c['dates'] = ','.join(member_post_dates(page, cfg, c['id'])) if c.get('id') else ''
                    payload = ';;'.join(f"{c['code']}｜{c['id']}｜{c['name']}｜{when}｜{c['dates']}" for c in new)
                    res = send(cfg, 'relay_comment', payload)
                    log(f'留言 {len(found)} 則、新 {len(new)} 則 → 後端：{res[:200]}')
                    try:
                        j = json.loads(res)
                        if j.get('ok'):
                            for c in new: st['sent'][c['code']] = when
                            st['recheck'] = j.get('recheck', [])
                            save_state(st)
                    except Exception: pass
                else:
                    log(f'留言含完賽碼 {len(found)} 則，沒有新的。')
                recheck_due(page, cfg, st)
                if feed_due(cfg, st):
                    try: feed_scan(page, cfg, st)
                    except Exception as e: log(f'掃社團出錯：{e!r}，下次再試')
            except Exception as e:
                log(f'這輪出錯：{e!r}，{cfg["INTERVAL_SEC"]} 秒後重試')
            time.sleep(int(cfg['INTERVAL_SEC']))

if __name__ == '__main__':
    main()
