"""スターバックス 葛西エリア アルバイト求人ウォッチャー

公式求人サイト(starbucks-job.jp)を実ブラウザで開き、葛西エリアの求人を職種名に関係なく全部拾う。
状態は state.json に保存し、出来事(新着・再掲載・掲載終了・エラー・復旧)を events に記録する。
通知は Claude のスケジュールタスクが events を読んで送る（NTFY_TOPIC を設定すれば ntfy でも送る）。
"""
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin

from playwright.sync_api import sync_playwright

JST = timezone(timedelta(hours=9))
BASE = "https://starbucks-job.jp"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
FORCE_TEST = os.environ.get("FORCE_TEST_NOTIFY") == "1"

# (ラベル, URL, 江戸川区ページか)
SOURCES = [
    ("江戸川区の一覧", f"{BASE}/jobfind-pc/area/Kanto/Tokyo/13123", True),
    ("葛西で検索", f"{BASE}/jobfind-pc/area/Kanto/All?freeword={quote('葛西')}", False),
    ("臨海で検索", f"{BASE}/jobfind-pc/area/Kanto/All?freeword={quote('臨海')}", False),
    ("江戸川で検索", f"{BASE}/jobfind-pc/area/Kanto/All?freeword={quote('江戸川')}", False),
]
REQUIRED = {"江戸川区の一覧", "葛西で検索"}   # この2つが両方読めた回だけ「掲載終了」を判定する
INCLUDE = ["葛西", "臨海公園", "レクリエーション公園", "4464", "4423", "清新町", "船堀"]
EXCLUDE = ["小岩", "一之江", "瑞江", "篠崎"]

EMPTY_TEXT = "該当する求人情報公開中店舗がありません"
BOT_TEXTS = ["confirm you are human", "Verify you are human", "人間であることを確認"]
CLOSE_CONFIRM_RUNS = 2        # 何回連続で消えていたら「掲載終了」とみなすか
FAIL_ALERT_RUNS = 3           # 何回連続で読めなかったら異常通知を出すか
FAIL_REALERT_HOURS = 12
RUN_BUDGET_SEC = 300


def now():
    return datetime.now(JST)


def norm(s):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "")).strip()


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def is_target(job, from_edogawa_page):
    text = norm(job["store"] + " " + job["card"])
    if any(x in text for x in EXCLUDE) and not any(x in text for x in INCLUDE):
        return False
    if from_edogawa_page:
        return True
    return any(x in text for x in INCLUDE)


# ---------------------------------------------------------------- 取得
# 求人詳細へのリンク(/jobfind-pc/job/...)ごとに、それを含む最小の「カード」要素の文字を拾う
JS_EXTRACT = """
() => {
  const out = [];
  const seen = new Set();
  document.querySelectorAll('a[href*="/jobfind-pc/job/"]').forEach(a => {
    const href = a.getAttribute('href') || '';
    const m = href.match(/\\/jobfind-pc\\/job\\/[^/]+\\/(\\d+)/);
    if (!m) return;
    // カード: 同じ番号へのリンクだけを含む一番大きい祖先
    let card = a;
    while (card.parentElement) {
      const p = card.parentElement;
      const ids = new Set([...p.querySelectorAll('a[href*="/jobfind-pc/job/"]')]
        .map(x => ((x.getAttribute('href') || '').match(/\\/job\\/[^/]+\\/(\\d+)/) || [])[1]).filter(Boolean));
      if (ids.size > 1 || p.tagName === 'BODY' || /件あり/.test(p.innerText)) break;
      card = p;
    }
    const text = a.innerText.trim();
    const id = m[1];
    if (!out.find(o => o.id === id)) out.push({id, href, store: '', card: card.innerText});
    const o = out.find(o => o.id === id);
    const dt = a.closest('dt');
    if (dt && text) o.store = text;               // 一覧カードの店舗名リンク
    else if (!o.store && text && !text.includes('詳細')) o.store = text;
  });
  return out;
}
"""


def read_listing(page, label, url):
    """1つの一覧URLを最後のページまで読み、(全求人dict, 表示件数) を返す。読めなければ例外。"""
    jobs, total, visited, next_url = {}, None, set(), url
    for _ in range(30):
        if next_url in visited:
            break
        visited.add(next_url)
        page.goto(next_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_function(
            "(t) => document.body && (document.body.innerText.includes('件あり') || document.body.innerText.includes(t)"
            " || /confirm you are human|Verify you are human/i.test(document.body.innerText))",
            arg=EMPTY_TEXT, timeout=25000,
        )
        page.wait_for_timeout(1500)
        body = page.inner_text("body")
        if any(b.lower() in body.lower() for b in BOT_TEXTS):
            raise RuntimeError(f"{label}: botチェックが表示された")
        if EMPTY_TEXT in body and "件あり" not in body:
            return {}, 0
        m = re.search(r"([0-9０-９,]+)\s*件あり", body)
        if not m:
            raise RuntimeError(f"{label}: 件数表示が読めない")
        total = int(norm(m.group(1)).replace(",", ""))
        for item in page.evaluate(JS_EXTRACT):
            card = norm(item["card"])
            wage = ""
            mw = re.search(r"(時給|日給|月給)\s*[0-9,，]+円(?:\s*[～~〜-]\s*[0-9,，]+円)?(?:以上)?", card)
            if mw:
                wage = norm(mw.group(0))
            mj = re.search(r"職種\s*[:：]?\s*(\S[^\n]{0,40})", item["card"] or "")
            jobs[item["id"]] = {
                "id": item["id"],
                "store": norm(item["store"]) or card[:40],
                "title": norm(mj.group(1)) if mj else "",
                "wage": wage,
                "card": unicodedata.normalize("NFKC", item["card"] or "")[:600],
                "url": f"{BASE}/jobfind-pc/job/Kanto/{item['id']}",
            }
        nxt = page.evaluate(
            """() => { const a=[...document.querySelectorAll('a')].find(x => x.innerText.trim().startsWith('次へ') && x.getAttribute('href') && x.getAttribute('href') !== '#'); return a ? a.href : null; }"""
        )
        if not nxt or len(jobs) >= total:
            break
        next_url = urljoin(BASE, nxt)
    if total is None:
        raise RuntimeError(f"{label}: 一覧が読めない")
    if len(jobs) < total:
        raise RuntimeError(f"{label}: 表示件数{total}件に対し{len(jobs)}件しか読めない")
    return jobs, total


def fetch_all():
    """戻り値: (対象求人dict, 必須2入口が両方読めたか, 1つでも読めたか, エラー文リスト)"""
    found, errors, ok_labels = {}, [], set()
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="ja-JP", timezone_id="Asia/Tokyo",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        )
        page = ctx.new_page()
        page.set_default_timeout(30000)
        deadline = time.monotonic() + RUN_BUDGET_SEC
        for label, url, edogawa in SOURCES:
            last_err = f"{label}: 時間切れで未確認"
            for attempt in range(3):
                if time.monotonic() > deadline:
                    break
                try:
                    jobs, total = read_listing(page, label, url)
                    hits = {k: v for k, v in jobs.items() if is_target(v, edogawa)}
                    print(f"[OK] {label}: 全{total}件 / 対象 {len(hits)}件 {sorted(hits)}")
                    for k, v in hits.items():
                        found.setdefault(k, v)
                    ok_labels.add(label)
                    last_err = None
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = f"{label}: {type(e).__name__}: {str(e).splitlines()[0][:160] if str(e) else ''}"
                    print(f"[RETRY {attempt + 1}/3] {last_err}")
                    time.sleep(5)
            if last_err:
                errors.append(last_err)
        # 職種の判定: 対象求人の詳細ページを開いて見る（見るだけ。応募はしない）
        excluded = {}
        for jid, job in list(found.items()):
            head = ""
            try:
                page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                head = page.evaluate(JS_DETAIL)
            except Exception as e:  # noqa: BLE001
                print(f"[詳細ページ読めず] {jid}: {type(e).__name__}")
            jtype, jtitle = classify(head, job["card"])
            job["type"], job["detail_head"] = jtype, norm(head)[:300]
            job["title"] = jtitle or ("" if job["title"] in ("すべて", "") else job["title"])
            print(f"[職種] {jid} {job['store']}: {jtype} / {job['title']}")
            if jtype not in WANT_TYPES:
                excluded[jid] = job
                del found[jid]
        browser.close()
    return found, excluded, REQUIRED <= ok_labels, bool(ok_labels), errors


# 詳細ページの「見出し」部分: ページタイトル・見出し・「職種」欄
JS_DETAIL = """
() => {
  const parts = [document.title];
  document.querySelectorAll('h1,h2,h3').forEach((h, i) => { if (i < 6) parts.push(h.innerText); });
  document.querySelectorAll('th,dt').forEach(el => {
    if (/職種/.test(el.innerText)) {
      const v = el.nextElementSibling;
      if (v) parts.push('職種:' + v.innerText);
    }
  });
  return parts.join('\\n');
}
"""

WANT_TYPES = {"バリスタ", "不明"}   # 通知する職種。不明は取りこぼし防止のため通知する
SHUFU = ["しゅふ", "主婦", "主夫"]
OTHER = ["シフトスーパーバイザー", "SSV", "アテンダント", "店長", "マネージャー"]


def classify(head, card):
    """(職種区分, 表示用の職種名) を返す。区分: バリスタ / しゅふ / その他 / 不明"""
    for text in (unicodedata.normalize("NFKC", head or ""), unicodedata.normalize("NFKC", card or "")):
        line = ""
        m = re.search(r"職種\s*[:：]?[ \t]*\n?[ \t]*([^\n]{1,60})", text)
        if m:
            line = norm(m.group(1))
        # 「職種」欄があればそれだけで判定（本文の「主婦歓迎」などに引っ張られない）
        target = line or text
        if not any(k in target for k in ["バリスタ"] + SHUFU + OTHER):
            continue
        name = line or next((w for w in re.findall(r"[^\s/|｜]*(?:バリスタ|スーパーバイザー|アテンダント)[^\s/|｜]*", target)), "")
        if any(k in target for k in SHUFU) and ("バリスタ" not in target or any(k in (line or name) for k in SHUFU)):
            return "しゅふ", name
        if any(k in (line or name) for k in OTHER):
            return "その他", name
        if "バリスタ" in target:
            return "バリスタ", name or "バリスタ"
        return "その他", name
    return "不明", ""


# ---------------------------------------------------------------- 通知（任意: ntfy）
def send_ntfy(msg):
    body = json.dumps({"topic": NTFY_TOPIC, **msg}, ensure_ascii=False).encode("utf-8")
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(NTFY_SERVER, data=body, method="POST",
                                         headers={"Content-Type": "application/json; charset=utf-8"})
            with urllib.request.urlopen(req, timeout=20) as r:
                if 200 <= r.status < 300:
                    return
                last = f"HTTP {r.status}"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"通知送信に失敗: {last}")


def job_msg(kind, job):
    head = {"new": "☕ スタバ（葛西エリア）にバイト求人が出ました",
            "reopen": "☕ スタバ葛西エリアの求人が再掲載",
            "closed": "スタバ葛西エリアの求人が掲載終了"}[kind]
    lines = [f"店舗: {job.get('store', '')}", f"職種: {job.get('title') or '(不明)'}"]
    if job.get("wage"):
        lines.append(f"時給: {job['wage']}")
    lines.append(job["url"])
    return {"title": head, "message": "\n".join(lines), "click": job["url"],
            "priority": 5 if kind != "closed" else 3}


# ---------------------------------------------------------------- 本体
def main():
    state = load_state()
    t = now()
    first_run = "jobs" not in state
    state.setdefault("jobs", {})
    state.setdefault("fail_streak", 0)
    state.setdefault("events", [])
    state.setdefault("outbox", [])

    def log_event(kind, job=None, note=""):
        e = {"ts": t.isoformat(), "kind": kind, "note": note}
        if job:
            e.update({k: job.get(k, "") for k in ("id", "store", "title", "wage", "url")})
        state["events"] = (state["events"] + [e])[-100:]

    try:
        found, excluded, all_ok, any_ok, errors = fetch_all()
    except Exception as e:  # noqa: BLE001
        found, excluded, all_ok, any_ok, errors = {}, {}, False, False, [f"ブラウザ起動失敗: {type(e).__name__}: {str(e)[:200]}"]
    # 対象外の職種（しゅふ向け・SSVなど）は記録から黙って外す（掲載終了の通知も出さない）
    state["excluded"] = {k: {x: v.get(x, "") for x in ("store", "title", "type", "url")} for k, v in excluded.items()}
    for jid in excluded:
        state["jobs"].pop(jid, None)
    msgs = []
    state["last_errors"] = errors[:4]

    if not any_ok:
        state["fail_streak"] += 1
        print("全入口で読み込み失敗:", errors)
        last_alert = state.get("last_fail_alert")
        due = state["fail_streak"] == FAIL_ALERT_RUNS or (
            state["fail_streak"] > FAIL_ALERT_RUNS and last_alert
            and t - datetime.fromisoformat(last_alert) >= timedelta(hours=FAIL_REALERT_HOURS))
        if due:
            note = " / ".join(errors)[:300]
            msgs.append({"title": "⚠️ スタバ求人の監視で確認できない状態が続いています", "message": note, "priority": 4})
            state["last_fail_alert"] = t.isoformat()
            log_event("error", note=note)
    else:
        if state["fail_streak"] >= FAIL_ALERT_RUNS:
            msgs.append({"title": "✅ スタバ求人の監視が復旧", "message": "求人ページを再び読めるようになりました。", "priority": 3})
            log_event("recovered")
        state["fail_streak"] = 0
        state["last_success_hour"] = t.strftime("%Y-%m-%dT%H:00+09:00")
        state["last_success"] = t.isoformat()
        state["last_full_check"] = t.isoformat() if all_ok else state.get("last_full_check", "")

        for jid, job in found.items():
            job = {**{k: v for k, v in job.items() if k != "card"}, "card_head": job["card"][:200]}
            prev = state["jobs"].get(jid)
            if prev is None:
                if not first_run:
                    msgs.append(job_msg("new", job))
                    log_event("new", job)
                state["jobs"][jid] = {**job, "status": "open", "miss": 0,
                                      "first_seen": t.isoformat(), "last_seen": t.isoformat()}
            else:
                if prev.get("status") == "closed":
                    msgs.append(job_msg("reopen", job))
                    log_event("reopen", job)
                prev.update({**job, "status": "open", "miss": 0, "last_seen": t.isoformat()})

        if all_ok:
            for jid, prev in state["jobs"].items():
                if prev.get("status") == "open" and jid not in found:
                    prev["miss"] = prev.get("miss", 0) + 1
                    if prev["miss"] >= CLOSE_CONFIRM_RUNS:
                        prev["status"] = "closed"
                        prev["closed_at"] = t.isoformat()
                        msgs.append(job_msg("closed", prev))
                        log_event("closed", prev)

        if first_run:
            log_event("started", note=",".join(sorted(found)) or "現在募集なし")

    if FORCE_TEST:
        msgs.append({"title": "🔔 テスト通知", "message": "通知テストです。", "priority": 4})

    if NTFY_TOPIC:
        queue, remaining = state["outbox"] + msgs, []
        for m in queue:
            try:
                send_ntfy(m)
            except Exception as e:  # noqa: BLE001
                print("通知失敗:", e)
                remaining.append(m)
        state["outbox"] = remaining[-50:]
    else:
        state["outbox"] = []
    save_state(state)
    if not any_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
