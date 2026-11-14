"""
고수 노트 — 관심·보유·상위 종목 상세 수집 (collect.py 다음에 실행)

대상: watch.json의 관심·보유 종목 + 기본 순위 상위 top_n
종목별로 data/focus/{code}.json 에 저장
  - 6개년 재무·수익성·안정성·현금흐름·주당지표·연말 PER/PBR/배당률 표
  - 적정주가 범위(그레이엄 수, PER·PBR·배당률 밴드)
  - 최근 공시(주요사항·위험 성격 표시), 최근 사업보고서 링크
  - 애널리스트 리포트, 뉴스
요약: data/focus_index.json, 알림: data/alerts.json (+ Gmail 발송, SMTP 시크릿이 있을 때)
"""
import json
import os
import re
import smtplib
import sys
import time
import datetime as dt
from email.mime.text import MIMEText
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect as C  # noqa: E402

DATA = C.DATA
FOC = DATA / "focus"
NOW = C.NOW
ROOT = DATA.parent

# 주요사항(희석·지배구조·사업) / 위험 성격 공시 키워드
KEY_KW = ["유상증자", "무상증자", "전환사채", "신주인수권", "교환사채", "감자", "합병", "분할", "영업양수", "영업양도",
          "최대주주", "자기주식", "자사주", "현금ㆍ현물배당", "현금배당", "주식소각", "단일판매", "공급계약", "잠정", "투자판단",
          "타법인주식", "유형자산", "공개매수", "대량보유"]
RISK_KW = ["소송", "횡령", "배임", "불성실", "관리종목", "거래정지", "매매거래정지", "감사의견", "회생", "상장폐지", "실질심사",
           "부도", "영업정지", "과징금", "검찰", "조회공시", "투자주의", "투자경고"]


def load_json(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


# ───────────────────────── 6개년 표 ─────────────────────────
def six_years(corp, Y, fcache, dcache):
    """fsx를 Y, Y-2, Y-4로 세 번 받아 6개년(당기·전기) 값을 만든다."""
    yrs = list(range(Y - 5, Y + 1))
    out = {k: [None] * 6 for k in ("rev", "gp", "op", "ni", "ctrl_ni", "assets", "liab", "equity", "ctrl_eq",
                                    "ca", "cl", "cash", "debt", "ocf", "capex", "divpaid")}
    for y in (Y, Y - 2, Y - 4):
        fx = C.fsx(corp, y, fcache) or {}
        for k in out:
            v = fx.get(k)
            if not v:
                continue
            for j, yy in enumerate((y, y - 1)):
                i = yrs.index(yy)
                if out[k][i] is None and j < len(v):
                    out[k][i] = v[j]
    da, db = C.dividends(corp, Y, dcache), C.dividends(corp, Y - 3, dcache)
    out["dps"] = [db["dps"][2], db["dps"][1], db["dps"][0], da["dps"][2], da["dps"][1], da["dps"][0]]
    return yrs, out


def year_end_prices(code, years):
    t = C.fchart(code, 1600)
    ye = {}
    for d, c in zip(t["d"], t["c"]):
        y = int(d[:4])
        if y in years:
            ye[y] = c  # 그 해 마지막 거래일 종가로 덮어씀
    return ye


def build_table(yrs, v, ye, shares, det):
    def div(a, b):
        return a / b if a is not None and b not in (None, 0) else None
    rows = []
    T = lambda lab, key, vals, unit="", d=0: rows.append({"g": key, "t": lab, "u": unit, "v": [None if x is None else round(x, d) for x in vals]})
    E = 1e8
    ni_c = [v["ctrl_ni"][i] if v["ctrl_ni"][i] is not None else v["ni"][i] for i in range(6)]
    eq_c = [v["ctrl_eq"][i] if v["ctrl_eq"][i] is not None else v["equity"][i] for i in range(6)]
    T("매출액", "성장", [div(x, E) for x in v["rev"]], "억")
    T("영업이익", "성장", [div(x, E) for x in v["op"]], "억")
    T("순이익(지배)", "성장", [div(x, E) for x in ni_c], "억")
    T("매출 증가율", "성장", [None] + [div(v["rev"][i] - v["rev"][i - 1], abs(v["rev"][i - 1])) * 100 if v["rev"][i] is not None and v["rev"][i - 1] else None for i in range(1, 6)], "%", 1)
    T("영업이익률", "수익성", [div(v["op"][i], v["rev"][i]) * 100 if div(v["op"][i], v["rev"][i]) is not None else None for i in range(6)], "%", 1)
    T("순이익률", "수익성", [div(ni_c[i], v["rev"][i]) * 100 if div(ni_c[i], v["rev"][i]) is not None else None for i in range(6)], "%", 1)
    T("ROE", "수익성", [div(ni_c[i], eq_c[i]) * 100 if div(ni_c[i], eq_c[i]) is not None else None for i in range(6)], "%", 1)
    T("ROA", "수익성", [div(ni_c[i], v["assets"][i]) * 100 if div(ni_c[i], v["assets"][i]) is not None else None for i in range(6)], "%", 1)
    T("부채비율", "안정성", [div(v["liab"][i], v["equity"][i]) * 100 if div(v["liab"][i], v["equity"][i]) is not None else None for i in range(6)], "%", 0)
    T("유동비율", "안정성", [div(v["ca"][i], v["cl"][i]) * 100 if div(v["ca"][i], v["cl"][i]) is not None else None for i in range(6)], "%", 0)
    T("순차입금", "안정성", [div(v["debt"][i] - v["cash"][i], E) if v["debt"][i] is not None and v["cash"][i] is not None else None for i in range(6)], "억")
    T("영업현금흐름", "현금흐름", [div(x, E) for x in v["ocf"]], "억")
    T("설비투자", "현금흐름", [div(x, E) for x in v["capex"]], "억")
    T("잉여현금흐름", "현금흐름", [div(v["ocf"][i] - (v["capex"][i] or 0), E) if v["ocf"][i] is not None else None for i in range(6)], "억")
    eps = [div(x, shares) for x in ni_c]
    bps = [div(x, shares) for x in eq_c]
    T("EPS", "주당", eps, "원")
    T("BPS", "주당", bps, "원")
    T("DPS", "주당", v["dps"], "원")
    T("배당성향", "주당", [div(v["dps"][i], eps[i]) * 100 if v["dps"][i] and eps[i] and eps[i] > 0 else None for i in range(6)], "%", 0)
    pr = [ye.get(y) for y in yrs]
    per = [div(pr[i], eps[i]) if eps[i] and eps[i] > 0 else None for i in range(6)]
    pbr = [div(pr[i], bps[i]) if bps[i] and bps[i] > 0 else None for i in range(6)]
    dyl = [div(v["dps"][i], pr[i]) * 100 if v["dps"][i] and pr[i] else None for i in range(6)]
    T("연말 주가", "밸류에이션", pr, "원")
    T("PER", "밸류에이션", per, "배", 1)
    T("PBR", "밸류에이션", pbr, "배", 2)
    T("배당수익률", "밸류에이션", dyl, "%", 2)
    # 최근 열: 최근 4분기·현재가 기준
    now = {"매출액": det.get("ttm", {}).get("rev"), "영업이익": det.get("ttm", {}).get("op"), "순이익(지배)": det.get("ttm", {}).get("ni"),
           "영업이익률": det.get("opm"), "ROE": det.get("roe"), "EPS": det.get("eps"), "BPS": det.get("bps"),
           "연말 주가": det.get("price"), "PER": det.get("per"), "PBR": det.get("pbr"), "배당수익률": det.get("dy")}
    for r in rows:
        r["now"] = now.get(r["t"])
    # 올해 분기 누적 열 (손익은 누적, 비율은 누적 기준, 부채비율은 분기말)
    q = det.get("q") or {}
    if q:
        def pc(a, b):
            return round(a / b * 100, 1) if a is not None and b else None
        rv, op, ni = q.get("rev") or [None, None], q.get("op") or [None, None], q.get("ni") or [None, None]
        qv = {"매출액": rv[0], "영업이익": op[0], "순이익(지배)": ni[0],
              "매출 증가율": round((rv[0] / rv[1] - 1) * 100, 1) if rv[0] is not None and rv[1] else None,
              "영업이익률": pc(op[0], rv[0]), "순이익률": pc(ni[0], rv[0]), "부채비율": q.get("debt")}
        for r in rows:
            r["q"] = qv.get(r["t"])
    return rows, per, pbr, dyl, eps, bps


def fair_value(price, eps_now, bps_now, dps_now, per_hist, pbr_hist, dy_hist):
    def med(xs):
        xs = sorted(x for x in xs if x is not None)
        if not xs:
            return None
        k = len(xs) // 2
        return xs[k] if len(xs) % 2 else (xs[k - 1] + xs[k]) / 2
    ms = []
    if eps_now and eps_now > 0 and bps_now and bps_now > 0:
        ms.append({"t": "그레이엄 수", "v": (22.5 * eps_now * bps_now) ** 0.5, "c": "√(22.5 × EPS × BPS)"})
    pm = med([x for x in per_hist if x and x < 60])
    if pm and eps_now and eps_now > 0 and sum(1 for x in per_hist if x and x < 60) >= 3:
        ms.append({"t": "PER 밴드", "v": pm * eps_now, "c": f"과거 PER 중앙값 {pm:.1f}배 × 현재 EPS"})
    bm = med([x for x in pbr_hist if x])
    if bm and bps_now and bps_now > 0 and sum(1 for x in pbr_hist if x) >= 3:
        ms.append({"t": "PBR 밴드", "v": bm * bps_now, "c": f"과거 PBR 중앙값 {bm:.2f}배 × 현재 BPS"})
    dm = med([x for x in dy_hist if x])
    if dm and dps_now and sum(1 for x in dy_hist if x) >= 4:
        ms.append({"t": "배당률 밴드", "v": dps_now / (dm / 100), "c": f"최근 DPS ÷ 과거 배당률 중앙값 {dm:.2f}%"})
    if not ms:
        return None
    vs = sorted(m["v"] for m in ms)
    lo, hi = vs[0], vs[-1]
    mid = med(vs)
    if hi > lo * 3:  # 방법 간 차이가 너무 크면 가운데 값 ±25%로 좁힌다
        lo, hi = mid * 0.75, mid * 1.25
    sig = "고평가" if price and price > hi else "저평가" if price and price < lo else "적정 범위"
    for m in ms:
        m["v"] = round(m["v"])
    return {"methods": ms, "lo": round(lo), "mid": round(mid), "hi": round(hi), "sig": sig,
            "gap": round((mid / price - 1) * 100, 1) if price else None}


# ───────────────────────── 공시·리포트·뉴스 ─────────────────────────
def disclosures(corp):
    end = NOW.strftime("%Y%m%d")
    bgn = (NOW - dt.timedelta(days=365)).strftime("%Y%m%d")
    j = C.dart("list.json", {"corp_code": corp, "bgn_de": bgn, "end_de": end, "page_count": 100})
    out, annual = [], None
    for it in (j or {}).get("list", []):
        nm = (it.get("report_nm") or "").strip()
        tag = "위험" if any(k in nm for k in RISK_KW) else "주요" if any(k in nm for k in KEY_KW) else ""
        rno = it.get("rcept_no")
        if annual is None and "사업보고서" in nm and "첨부" not in nm:
            annual = {"t": nm, "d": it.get("rcept_dt"), "u": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rno}"}
        out.append({"t": nm, "d": it.get("rcept_dt"), "by": it.get("flr_nm"), "tag": tag, "no": rno,
                    "u": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rno}"})
    return out[:60], annual


def _txt(h):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h or "")).strip()


DIAG = {"reports": 0, "news": 0}


def _diag(kind, html):
    """처음 한 번만, 결과가 0건일 때 응답 모양을 로그에 남긴다 (형식 변경 확인용)."""
    if DIAG[kind]:
        return
    DIAG[kind] = 1
    hrefs = re.findall(r'href="([^"]{0,120})"', html)[:8]
    C.log(f"  [{kind} 0건 진단] 응답 {len(html)}자, 링크 예시: {hrefs}")


def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def reports(code):
    out = []
    try:
        r = C.naver_get("https://finance.naver.com/research/company_list.naver", {"searchType": "itemCode", "itemCode": code, "page": 1})
        html = r.content.decode("euc-kr", errors="replace")
    except Exception:  # noqa: BLE001
        html = ""
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        m = re.search(r'href="[^"]*company_read\.naver\?nid=(\d+)[^"]*"[^>]*>(.*?)</a>', tr, re.S)
        if not m:
            continue
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        pdf = re.search(r'href="(https?://[^"]+\.pdf)"', tr, re.I)
        date = next((_txt(t) for t in tds if re.fullmatch(r"\d{2}\.\d{2}\.\d{2}", _txt(t))), "")
        broker = _txt(tds[2]) if len(tds) > 2 else ""
        out.append({"t": _txt(m.group(2)), "by": broker, "d": date,
                    "u": f"https://finance.naver.com/research/company_read.naver?nid={m.group(1)}",
                    "pdf": pdf.group(1) if pdf else None})
    if not out:
        _diag("reports", html)
    return out[:15]


def news(code):
    out, seen = [], set()
    # 1) 네이버 금융 종목 뉴스 (HTML)
    try:
        r = C.S.get("https://finance.naver.com/item/news_news.naver", params={"code": code, "page": 1, "clusterId": ""}, timeout=20,
                    headers={**C.NHDR, "Referer": f"https://finance.naver.com/item/news.naver?code={code}"})
        html = r.content.decode("euc-kr", errors="replace")
    except Exception:  # noqa: BLE001
        html = ""
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        m = re.search(r'href="[^"]*?article_id=(\d+)(?:&|&amp;)office_id=(\d+)[^"]*"[^>]*>(.*?)</a>', tr, re.S) or \
            re.search(r'href="[^"]*?office_id=(\d+)(?:&|&amp;)article_id=(\d+)[^"]*"[^>]*>(.*?)</a>', tr, re.S)
        if not m:
            continue
        aid, oid = (m.group(1), m.group(2)) if "article_id=" + m.group(1) in m.group(0) else (m.group(2), m.group(1))
        if aid in seen:
            continue
        seen.add(aid)
        src = re.search(r'class="info"[^>]*>(.*?)</td>', tr, re.S)
        dd = re.search(r'class="date"[^>]*>(.*?)</td>', tr, re.S)
        out.append({"t": _txt(m.group(3)), "by": _txt(src.group(1)) if src else "", "d": _txt(dd.group(1)) if dd else "",
                    "u": f"https://n.news.naver.com/mnews/article/{oid}/{aid}"})
    # 2) 모바일 증권 API (JSON) — 1)이 비었을 때
    if not out:
        try:
            j = C.S.get(f"https://m.stock.naver.com/api/news/stock/{code}", params={"pageSize": 20, "page": 1}, timeout=20, headers=C.NHDR).json()
            for o in _walk(j):
                aid, oid, t = o.get("articleId"), o.get("officeId"), o.get("title") or o.get("titleFull")
                if aid and oid and t and aid not in seen:
                    seen.add(aid)
                    dtm = str(o.get("datetime") or o.get("dt") or "")
                    out.append({"t": _txt(t), "by": o.get("officeName") or "", "d": f"{dtm[:4]}.{dtm[4:6]}.{dtm[6:8]}" if len(dtm) >= 8 else dtm,
                                "u": f"https://n.news.naver.com/mnews/article/{oid}/{aid}"})
        except Exception:  # noqa: BLE001
            pass
    if not out:
        _diag("news", html)
    return out[:20]


# ───────────────────────── 알림 ─────────────────────────
def send_mail(subject, html):
    user, pw, to = os.environ.get("SMTP_USER", "").strip(), os.environ.get("SMTP_APP_PASSWORD", "").replace(" ", "").strip(), os.environ.get("MAIL_TO", "").strip()
    if not (user and pw):
        C.log("  알림 메일: SMTP 시크릿이 없어 건너뜀 (앱 관심 탭에는 표시)")
        return
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to or user
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.starttls(); s.login(user, pw); s.send_message(msg)
    C.log(f"  알림 메일 발송: {subject}")


def main():
    latest = load_json(DATA / "latest.json", None)
    details = load_json(DATA / "details.json", {})
    uni = load_json(DATA / "universe.json", {}).get("u", {})
    if not latest:
        sys.exit("[중단] latest.json이 없습니다. collect.py가 먼저 실행돼야 합니다.")
    watch = load_json(ROOT / "watch.json", {})
    wcodes, hcodes = watch.get("watch", []), watch.get("hold", [])
    top = [c["code"] for c in latest["candidates"]]
    focus = list(dict.fromkeys(wcodes + hcodes + top))
    C.log(f"상세 수집 대상: 관심 {len(wcodes)} · 보유 {len(hcodes)} · 상위 {len(top)} → {len(focus)}종목")
    Y = latest["fiscal_year"]
    cmap = C.corp_codes()
    fcache, dcache = C.load_cache("fsx"), C.load_cache("div")
    FOC.mkdir(parents=True, exist_ok=True)
    prev_idx = load_json(DATA / "focus_index.json", {})
    idx = {}
    prices = load_json(DATA / "prices.json", {}).get("p", {})
    for i, code in enumerate(focus, 1):
        corp = cmap.get(code)
        det = details.get(code) or {}
        pr = prices.get(code)
        if not corp or not pr:
            continue
        name, price, mcap = pr[0], pr[1], pr[2]
        shares = mcap * 1e8 / price if price else None
        rec = {"code": code, "name": name, "updated": NOW.strftime("%Y-%m-%d"), "price": price}
        try:
            yrs, v = six_years(corp, Y, fcache, dcache)
            ye = year_end_prices(code, yrs)
            rows, per_h, pbr_h, dy_h, eps_h, bps_h = build_table(yrs, v, ye, shares, det)
            rec["years"], rec["table"] = yrs, rows
            rec["qlab"] = (det.get("q") or {}).get("lab")
            eps_now = det.get("eps") if det.get("eps") is not None else eps_h[-1]
            bps_now = det.get("bps") if det.get("bps") is not None else bps_h[-1]
            rec["fv"] = fair_value(price, eps_now, bps_now, v["dps"][-1], per_h, pbr_h, dy_h)
        except Exception as e:  # noqa: BLE001
            C.log(f"  {name} 재무표 실패: {e}")
        disc, annual = disclosures(corp)
        rec["disc"], rec["annual"] = disc, annual
        rec["reports"] = reports(code)
        rec["news"] = news(code)
        time.sleep(0.2)
        C.write_json(FOC / f"{code}.json", rec)
        recent = [d for d in disc if d["tag"] and d["d"] and d["d"] >= (NOW - dt.timedelta(days=14)).strftime("%Y%m%d")]
        idx[code] = {"n": name, "fv": {k: rec["fv"][k] for k in ("lo", "mid", "hi", "sig", "gap")} if rec.get("fv") else None,
                     "kd": [d["no"] for d in disc if d["tag"]][:20], "rk": len(recent), "nr": len(rec["reports"]), "nn": len(rec["news"])}
        if i % 20 == 0:
            C.save_cache("fsx", fcache); C.save_cache("div", dcache); C.log(f"  상세 {i}/{len(focus)}")
    C.save_cache("fsx", fcache); C.save_cache("div", dcache)
    for p in FOC.glob("*.json"):  # 대상에서 빠진 종목 파일 정리
        if p.stem not in idx:
            p.unlink()
    C.write_json(DATA / "focus_index.json", idx)

    # 알림: 관심·보유 종목만
    try:
        bd = C.S.get("https://dukong2-stack.github.io/badak/data/latest.json", timeout=20).json()
        stages = {it["code"]: it.get("stage", 0) for it in bd.get("items", [])}
    except Exception:  # noqa: BLE001
        stages = {}
    state = load_json(DATA / "alert_state.json", {})
    alerts = load_json(DATA / "alerts.json", [])
    events = []
    today = NOW.strftime("%Y-%m-%d")
    for code in dict.fromkeys(wcodes + hcodes):
        u, f = uni.get(code, {}), idx.get(code, {})
        name = u.get("n") or f.get("n") or code
        old = state.get(code)
        cur = {"st": stages.get(code, 0), "sc": u.get("sc"), "sig": (f.get("fv") or {}).get("sig"), "kd": f.get("kd", []),
               "out": bool(u.get("f"))}
        if old:
            if cur["st"] and cur["st"] > (old.get("st") or 0):
                events.append((code, name, "바닥 신호", f"{['', '① 과매도', '② 바닥 형성', '③ 전환 확인'][min(cur['st'], 3)]} 진입"))
            if cur["sc"] is not None and old.get("sc") is not None and abs(cur["sc"] - old["sc"]) >= 10:
                events.append((code, name, "점수 변화", f"{old['sc']:.0f}점 → {cur['sc']:.0f}점"))
            if cur["sig"] and cur["sig"] != old.get("sig") and cur["sig"] in ("고평가", "저평가"):
                events.append((code, name, "적정가", f"{cur['sig']} 구간 진입"))
            new = [no for no in cur["kd"] if no not in (old.get("kd") or [])]
            if new:
                fj = load_json(FOC / f"{code}.json", {})
                titles = [d["t"] for d in fj.get("disc", []) if d["no"] in new][:3]
                events.append((code, name, "공시", ", ".join(titles) or f"주요 공시 {len(new)}건"))
            if cur["out"] and not old.get("out"):
                events.append((code, name, "걸러내기 탈락", ", ".join(u.get("f", []))))
        state[code] = cur
    C.write_json(DATA / "alert_state.json", state)
    if events:
        alerts = [{"d": today, "code": c, "n": n, "k": k, "t": t} for c, n, k, t in events] + alerts
        rows = "".join(f"<tr><td style='padding:6px 10px'><b>{n}</b></td><td style='padding:6px 10px'>{k}</td><td style='padding:6px 10px'>{t}</td></tr>" for _, n, k, t in events)
        try:
            send_mail(f"[고수 노트] 관심 종목 알림 {len(events)}건 ({today})",
                      f"<h3>고수 노트 알림</h3><table style='border-collapse:collapse;font-size:14px'>{rows}</table>"
                      f"<p><a href='https://dukong2-stack.github.io/gosu/'>앱 열기</a></p>")
        except Exception as e:  # noqa: BLE001
            C.log(f"  알림 메일 실패: {e}")
    C.write_json(DATA / "alerts.json", alerts[:100])
    nn = sum(1 for v in idx.values() if v["nn"]); nr = sum(1 for v in idx.values() if v["nr"])
    C.log(f"완료: 상세 {len(idx)}종목 (뉴스 있음 {nn}, 리포트 있음 {nr}), 알림 {len(events)}건 (DART 호출 {C.CALLS['dart']}회)")


if __name__ == "__main__":
    main()
