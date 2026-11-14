"""
고수 노트 — 전문가 방식 종목 선정 수집 스크립트 (배당가치 노트에서 복사해 만든 별도 앱)

적용한 전문가 방식
  - 그린블라트 마법공식: 이익수익률(EBIT/EV) + 자본수익률(EBIT/(순운전자본+유형자산)) 순위
  - 그레이엄 안전성: 이익 안정·배당·이익 성장·유동비율·재무안정·PER·PBR 7개 항목
  - 피오트로스키 F-스코어: 수익성 4·재무구조 3·효율성 2 = 9개 항목
  - 피터 린치 PEG: PER ÷ 이익 성장률, 성장/정체/하향 구분
  - 오닐 추세: 12-1개월 수익률, 200일선, 52주 신고가 근접도

데이터 출처
  - 주가·시가총액: 공공데이터포털(DATA_GO_KR_KEY) → 실패 시 네이버 시세
  - 일봉(추세·거래대금): 네이버 차트
  - 재무·업종·공시: OpenDART (DART_API_KEY)

결과물 (data/)
  latest.json    상위 후보 상세(점수·항목별 판정·수치)
  universe.json  평가한 전체 종목의 점수·순위·탈락 사유 (보유 종목 점검용)
  prices.json    전 종목 최근 종가
  history/       월별 상위 후보 스냅샷 (선정 이후 성과 검증용)
  cache/         변하지 않는 공시 데이터 캐시
"""
import os, io, re, sys, json, time, math, zipfile, statistics, datetime as dt
from pathlib import Path
from urllib.parse import unquote
import xml.etree.ElementTree as ET
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "cache"
HIST = DATA / "history"
for p in (DATA, CACHE, HIST):
    p.mkdir(parents=True, exist_ok=True)

DART = "https://opendart.fss.or.kr/api"
PRICE_URL = "https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo"
DART_KEY = os.environ.get("DART_API_KEY", "").strip()
GO_KEY = unquote(os.environ.get("DATA_GO_KR_KEY", "").strip())  # Encoding 키를 넣어도 동작하도록
SLEEP = float(os.environ.get("DART_SLEEP", "0.12"))
KST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime.now(KST)
S = requests.Session()
S.headers["User-Agent"] = "divnote/1.0"
CALLS = {"dart": 0}

CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

SECTORS = {
    "01": "농업", "02": "임업", "03": "어업", "05": "석탄·원유", "06": "금속광업", "07": "비금속광업", "08": "광업지원",
    "10": "식료품", "11": "음료", "12": "담배", "13": "섬유", "14": "의복", "15": "가죽·신발", "16": "목재",
    "17": "펄프·종이", "18": "인쇄", "19": "석유정제", "20": "화학", "21": "의약품", "22": "고무·플라스틱",
    "23": "비금속광물", "24": "1차금속", "25": "금속가공", "26": "전자부품·통신장비", "27": "의료·정밀기기",
    "28": "전기장비", "29": "기계", "30": "자동차·부품", "31": "조선·운송장비", "32": "가구", "33": "기타제조",
    "35": "전기·가스", "36": "수도", "37": "하수처리", "38": "폐기물·재활용", "41": "종합건설", "42": "전문건설",
    "45": "자동차판매", "46": "도매", "47": "소매", "49": "육상운송", "50": "해운", "51": "항공운송",
    "52": "물류·창고", "55": "숙박", "56": "음식점", "58": "출판·소프트웨어", "59": "영상·음악", "60": "방송",
    "61": "통신", "62": "IT서비스", "63": "정보서비스", "64": "금융", "65": "보험", "66": "금융서비스",
    "68": "부동산", "70": "연구개발", "71": "전문서비스", "72": "엔지니어링", "73": "과학기술서비스",
    "74": "시설관리", "75": "사업지원", "84": "공공행정", "85": "교육", "86": "보건", "87": "사회복지",
    "90": "예술", "91": "스포츠·오락", "94": "협회·단체", "95": "수리", "96": "개인서비스",
}
FINANCIAL = {"64", "65", "66"}
CYCLICAL = {"05", "06", "07", "08", "19", "20", "24", "31", "41", "42", "50"}


def log(*a):
    print(*a, flush=True)


# ───────────────────────── 공통 유틸 ─────────────────────────
def num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").replace(" ", "").strip()
    if s in ("", "-", "N/A", "nan"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        x = float(s)
    except ValueError:
        return None
    return -x if neg else x


def load_cache(name):
    f = CACHE / f"{name}.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(name, obj):
    (CACHE / f"{name}.json").write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def write_json(path, obj, pretty=False):
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=1 if pretty else None, separators=None if pretty else (",", ":")),
        encoding="utf-8",
    )


def dart(path, params, raw=False):
    """OpenDART 호출. 데이터 없음(013)은 None, 키·한도 오류는 즉시 중단."""
    q = {"crtfc_key": DART_KEY, **params}
    last = None
    for i in range(4):
        try:
            r = S.get(f"{DART}/{path}", params=q, timeout=40)
            CALLS["dart"] += 1
            time.sleep(SLEEP)
            if raw:
                r.raise_for_status()
                return r.content
            j = r.json()
            st = j.get("status")
            if st == "000":
                return j
            if st in ("013", "014"):
                return None
            if st in ("010", "011", "012", "901"):
                sys.exit(f"[중단] OpenDART 인증키 문제({st}): {j.get('message')} — 저장소 Secrets의 DART_API_KEY를 확인하세요.")
            if st == "020":
                sys.exit("[중단] OpenDART 일일 호출 한도를 넘었습니다. 내일 다시 실행하면 캐시 덕분에 이어서 진행됩니다.")
            if st == "800":
                sys.exit("[중단] OpenDART 시스템 점검 중입니다.")
            last = f"{st} {j.get('message')}"
            log(f"  DART {path} 응답 {last}")
            return None
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(2 * (i + 1))
    log(f"  DART {path} 실패: {last}")
    return None


# ───────────────────────── 1. 주가 ─────────────────────────
NAVER_URL = "https://m.stock.naver.com/api/stocks/marketValue/{mkt}"


def fetch_prices_go():
    """공공데이터포털 주식시세. 실패하면 예외를 던져 네이버로 넘어감"""
    if not GO_KEY:
        raise RuntimeError("DATA_GO_KR_KEY 없음")
    for back in range(0, 12):
        d = (NOW - dt.timedelta(days=back)).strftime("%Y%m%d")
        rows, page, total = [], 1, 0
        while True:
            r = S.get(PRICE_URL, params={
                "serviceKey": GO_KEY, "resultType": "json", "numOfRows": 5000, "pageNo": page, "basDt": d,
            }, timeout=60)
            text = r.text.strip()
            try:
                j = r.json()
            except ValueError:
                raise RuntimeError(f"JSON 아님 (HTTP {r.status_code}): {text[:300]}")
            resp = j.get("response", j) if isinstance(j, dict) else {}
            hdr = resp.get("header") or {}
            body = resp.get("body")
            if body is None or str(hdr.get("resultCode", "00")) != "00":
                raise RuntimeError(f"HTTP {r.status_code}: {text[:300]}")
            total = int(body.get("totalCount") or 0)
            items = body.get("items") or {}
            items = items.get("item", []) if isinstance(items, dict) else items
            if isinstance(items, dict):
                items = [items]
            rows += items
            if len(rows) >= total or not items:
                break
            page += 1
        if rows:
            return d, rows
    raise RuntimeError("최근 12일 동안 데이터 없음")


def fetch_prices_naver():
    """네이버 증권 공개 시세(시가총액순). 인증키 불필요"""
    hdr = {"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"}
    rows = []
    for mkt in ("KOSPI", "KOSDAQ"):
        for page in range(1, 61):
            try:
                r = S.get(NAVER_URL.format(mkt=mkt), params={"page": page, "pageSize": 100}, headers=hdr, timeout=30)
                j = r.json()
            except Exception as e:
                sys.exit(f"[중단] 네이버 시세 응답을 읽지 못했습니다 ({mkt} {page}쪽): {e}")
            items = j.get("stocks") if isinstance(j, dict) else j
            if not items:
                break
            for it in items:
                code = str(it.get("itemCode") or "").strip()
                if len(code) != 6:
                    continue
                if str(it.get("stockEndType") or "stock").lower() != "stock":
                    continue  # ETF·ETN 등 제외
                mv = num(it.get("marketValue"))
                if mv is not None and mv < 1e9:  # 억원 단위면 원으로 환산
                    mv *= 1e8
                rows.append({"srtnCd": code, "itmsNm": it.get("stockName") or it.get("itemName"),
                             "clpr": num(it.get("closePrice")), "mrktTotAmt": mv, "mrktCtg": mkt})
            total = j.get("totalCount") if isinstance(j, dict) else None
            if total and page * 100 >= int(total):
                break
            time.sleep(0.2)
    if len(rows) < 1000:
        sys.exit(f"[중단] 네이버 시세가 {len(rows)}종목만 수집되었습니다. 이 로그를 보내 주세요.")
    d = NOW - dt.timedelta(days=1) if NOW.hour < 16 else NOW  # 장 마감 전 실행이면 직전 거래일 종가
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.strftime("%Y%m%d"), rows


def fetch_prices():
    try:
        d, rows = fetch_prices_go()
        log(f"주가(공공데이터포털) 기준일 {d}: {len(rows)}종목")
        return d, rows
    except Exception as e:
        log(f"공공데이터포털 실패 → 네이버 시세로 전환 ({str(e)[:150]})")
    d, rows = fetch_prices_naver()
    log(f"주가(네이버) {d}: {len(rows)}종목")
    return d, rows


# ───────────────────────── 2. 고유번호 ─────────────────────────
def corp_codes():
    f = CACHE / "corpcode.json"
    if f.exists() and (time.time() - f.stat().st_mtime) < 7 * 86400:
        return json.loads(f.read_text(encoding="utf-8"))
    raw = dart("corpCode.xml", {}, raw=True)
    if not raw:
        sys.exit("[중단] OpenDART 고유번호 파일을 받지 못했습니다.")
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        sys.exit(f"[중단] 고유번호 파일 형식 오류: {raw[:200]!r} — 인증키를 확인하세요.")
    root = ET.fromstring(z.read(z.namelist()[0]))
    m = {}
    for el in root.iter("list"):
        sc = (el.findtext("stock_code") or "").strip()
        if sc:
            m[sc] = el.findtext("corp_code").strip()
    f.write_text(json.dumps(m), encoding="utf-8")
    return m


# ───────────────────────── 3. 재무 주요계정 ─────────────────────────
ACC = {
    "rev": ("매출액", "영업수익", "수익(매출액)"),
    "op": ("영업이익",),
    "ni": ("당기순이익",),
    "assets": ("자산총계",),
    "liab": ("부채총계",),
    "equity": ("자본총계",),
}


def pick_acc(nm):
    for k, keys in ACC.items():
        if any(x in nm for x in keys):
            if k == "rev" and "원가" in nm:
                continue
            return k
    return None


def multi_accounts(corps, year, reprt):
    """다중회사 주요계정(100개씩). 반환: {corp: {acc: [당기, 전기, 전전기], 'q': {...}}}"""
    name = f"fin2_{year}_{reprt}" if reprt == "11011" else f"finq_{year}_{reprt}"
    cache = load_cache(name)
    todo = [c for c in corps if c not in cache]
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        j = dart("fnlttMultiAcnt.json", {"corp_code": ",".join(chunk), "bsns_year": str(year), "reprt_code": reprt})
        if not j:
            continue
        per = {}
        for it in j.get("list", []):
            per.setdefault(it["corp_code"], []).append(it)
        for c, items in per.items():
            has_cfs = any(it.get("fs_div") == "CFS" for it in items)
            out = {}
            for it in items:
                if reprt == "11011" and has_cfs and it.get("fs_div") == "OFS" and pick_acc(it.get("account_nm", "")) == "rev" and "rev_ofs" not in out:
                    out["rev_ofs"] = [num(it.get("thstrm_amount")), num(it.get("frmtrm_amount")), num(it.get("bfefrmtrm_amount"))]
                if (it.get("fs_div") == "CFS") != has_cfs:
                    continue
                k = pick_acc(it.get("account_nm", ""))
                if not k or k in out:
                    continue
                if reprt == "11011":
                    out[k] = [num(it.get("thstrm_amount")), num(it.get("frmtrm_amount")), num(it.get("bfefrmtrm_amount"))]
                else:  # 분기·반기: 누적 금액 우선
                    cur = num(it.get("thstrm_add_amount")) if it.get("thstrm_add_amount") not in (None, "", "-") else num(it.get("thstrm_amount"))
                    prv = num(it.get("frmtrm_add_amount")) if it.get("frmtrm_add_amount") not in (None, "", "-") else num(it.get("frmtrm_amount"))
                    q_cur = num(it.get("thstrm_amount")) if it.get("thstrm_add_amount") not in (None, "", "-") else None
                    q_prv = num(it.get("frmtrm_amount")) if it.get("frmtrm_add_amount") not in (None, "", "-") else None
                    out[k] = [cur, prv, q_cur, q_prv]  # [당기 누적, 전기 누적, 당기 3개월, 전기 3개월]
            out["fs"] = "CFS" if has_cfs else "OFS"
            cache[c] = out
        save_cache(name, cache)
        log(f"  주요계정 {year}/{reprt}: {min(i + 100, len(todo))}/{len(todo)}")
    return cache




# ───────────────────────── 5. 업종·현금흐름 ─────────────────────────
def company(corp, cache):
    if corp in cache:
        return cache[corp]
    j = dart("company.json", {"corp_code": corp})
    out = {"ksic": (j or {}).get("induty_code", ""), "cls": (j or {}).get("corp_cls", "")}
    cache[corp] = out
    return out


def quarter_candidates():
    md = NOW.month * 100 + NOW.day
    return [x for x in ((NOW.year, "11014", "3분기보고서", 3, 1101), (NOW.year, "11012", "반기보고서", 2, 801),
                        (NOW.year, "11013", "1분기보고서", 1, 501)) if md >= x[4]]



# ───────────────────────── 지표 계산 ─────────────────────────
def series(fin_a, fin_b, acc):
    """[Y-5, ..., Y] 6개년 시계열"""
    a = (fin_a or {}).get(acc) or [None] * 3
    b = (fin_b or {}).get(acc) or [None] * 3
    return [b[2], b[1], b[0], a[2], a[1], a[0]]


def cagr(a, b, n):
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return (b / a) ** (1 / n) - 1


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def r1(x, d=1):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, d)


def pct_rank(values, higher_better=True):
    """None은 중립(0.5). 반환: 0~1"""
    idx = [i for i, v in enumerate(values) if v is not None]
    out = [0.5] * len(values)
    if len(idx) <= 1:
        return out
    srt = sorted(idx, key=lambda i: values[i])
    n = len(srt)
    for rank, i in enumerate(srt):
        p = rank / (n - 1)
        out[i] = p if higher_better else 1 - p
    return out


RISK_KW = [("상장적격성", "상장적격성 실질심사"), ("실질심사", "상장적격성 실질심사"), ("불성실공시법인", "불성실공시법인 지정"),
           ("관리종목", "관리종목"), ("투자주의환기", "투자주의환기종목"), ("감사의견", "감사의견 관련"), ("감사범위제한", "감사범위 제한"),
           ("횡령", "횡령·배임"), ("배임", "횡령·배임"), ("과징금", "과징금"), ("감리", "감리 관련"), ("검찰", "검찰 고발·통보"),
           ("회생절차", "회생절차"), ("상장폐지", "상장폐지 관련")]


def risk_history(corp, bgn, end):
    """최근 3년 거래소 공시 중 위험 신호"""
    hits = {}
    for page in (1, 2, 3):
        j = dart("list.json", {"corp_code": corp, "bgn_de": bgn, "end_de": end, "pblntf_ty": "I", "page_no": str(page), "page_count": "100"})
        items = (j or {}).get("list", [])
        for it in items:
            key = (it.get("report_nm") or "").replace(" ", "")
            labs = {lab for kw, lab in RISK_KW if kw in key}
            if "매매거래정지" in key and "중요내용공시" not in key:
                labs.add("매매거래정지")
            if "감사의견" in key and "적정" in key and "비적정" not in key and "부적정" not in key:
                labs.discard("감사의견 관련")
            for lab in labs:
                y = (it.get("rcept_dt") or "")[:4]
                hits.setdefault(lab, set()).add(y)
        if not j or int(j.get("total_page") or 1) <= page:
            break
    return [{"t": lab, "y": sorted(ys)} for lab, ys in sorted(hits.items())]




# ───────────────────────── 네이버: 일봉·관리종목 ─────────────────────────
NHDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Referer": "https://finance.naver.com/"}


def naver_get(url, params=None, tries=3):
    last = None
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=25, headers=NHDR)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def fchart(code, count=260):
    """네이버 일봉(수정주가). 반환: {d:[], c:[], v:[]}"""
    out = {"d": [], "c": [], "v": []}
    try:
        r = naver_get("https://fchart.stock.naver.com/sise.nhn",
                      {"symbol": code, "timeframe": "day", "count": count, "requestType": 0})
    except Exception:  # noqa: BLE001
        return out
    for row in re.findall(r'data="([^"]+)"', r.content.decode("euc-kr", errors="replace")):
        p = row.split("|")
        if len(p) < 6:
            continue
        c, v = num(p[4]), num(p[5])
        if not c or c <= 0:
            continue
        out["d"].append(p[0]); out["c"].append(c); out["v"].append(v or 0)
    return out


def flagged_naver():
    """관리종목·거래정지·투자경고·투자위험 종목코드 → 사유"""
    pages = [("관리종목", "https://finance.naver.com/sise/management.naver", None),
             ("거래정지", "https://finance.naver.com/sise/trading_halt.naver", None),
             ("투자경고", "https://finance.naver.com/sise/investment_alert.naver", {"type": "warning"}),
             ("투자위험", "https://finance.naver.com/sise/investment_alert.naver", {"type": "risk"})]
    out = {}
    for why, url, params in pages:
        try:
            r = naver_get(url, params)
            for code in re.findall(r"/item/main\.naver\?code=(\d{6})", r.content.decode("euc-kr", errors="replace")):
                out.setdefault(code, why)
        except Exception as e:  # noqa: BLE001
            log(f"  네이버 {why} 목록 실패: {e}")
        time.sleep(0.3)
    return out


# ───────────────────────── 전체 재무제표에서 필요한 값 ─────────────────────────
def _is(aid, nm, ids, names):
    return aid in ids or nm in names


def fsx(corp, year, cache):
    """사업보고서 전체 재무제표(연결 우선)에서 당기·전기 값. 반환: {키: [당기, 전기]}"""
    key = f"{corp}_{year}"
    if key in cache:
        return cache[key]
    out = None
    for fs in ("CFS", "OFS"):
        j = dart("fnlttSinglAcntAll.json", {"corp_code": corp, "bsns_year": str(year), "reprt_code": "11011", "fs_div": fs})
        if not j:
            continue
        o = {"fs": fs}
        sums = {"cash": [0.0, 0.0], "debt": [0.0, 0.0], "st_debt": [0.0, 0.0]}
        seen_cash = False

        def put(k, v):
            if k not in o and v[0] is not None:
                o[k] = v
        for it in j.get("list", []):
            aid = it.get("account_id", "")
            nm = (it.get("account_nm") or "").replace(" ", "")
            sj = it.get("sj_div")
            v = [num(it.get("thstrm_amount")), num(it.get("frmtrm_amount"))]
            if sj in ("IS", "CIS"):
                if _is(aid, nm, ("ifrs-full_Revenue",), ("매출액", "수익(매출액)", "영업수익", "매출")):
                    put("rev", v)
                elif _is(aid, nm, ("ifrs-full_GrossProfit",), ("매출총이익", "매출총이익(손실)")):
                    put("gp", v)
                elif _is(aid, nm, ("dart_OperatingIncomeLoss",), ("영업이익", "영업이익(손실)")):
                    put("op", v)
                elif _is(aid, nm, ("ifrs-full_ProfitLoss",), ("당기순이익", "당기순이익(손실)")):
                    put("ni", v)
                elif aid == "ifrs-full_ProfitLossAttributableToOwnersOfParent" or ("지배기업" in nm and "비지배" not in nm and ("순이익" in nm or "순손익" in nm)):
                    put("ctrl_ni", v)
            elif sj == "BS":
                if _is(aid, nm, ("ifrs-full_Assets",), ("자산총계",)):
                    put("assets", v)
                elif _is(aid, nm, ("ifrs-full_CurrentAssets",), ("유동자산",)):
                    put("ca", v)
                elif _is(aid, nm, ("ifrs-full_CurrentLiabilities",), ("유동부채",)):
                    put("cl", v)
                elif _is(aid, nm, ("ifrs-full_NoncurrentLiabilities",), ("비유동부채",)):
                    put("ncl", v)
                elif _is(aid, nm, ("ifrs-full_Liabilities",), ("부채총계",)):
                    put("liab", v)
                elif _is(aid, nm, ("ifrs-full_Equity",), ("자본총계",)):
                    put("equity", v)
                elif aid == "ifrs-full_EquityAttributableToOwnersOfParent" or ("지배기업" in nm and "비지배" not in nm and "지분" in nm):
                    put("ctrl_eq", v)
                elif _is(aid, nm, ("ifrs-full_PropertyPlantAndEquipment",), ("유형자산",)):
                    put("ppe", v)
                elif _is(aid, nm, ("ifrs-full_IssuedCapital",), ("자본금",)):
                    put("cap", v)
                for k in (0, 1):
                    x = v[k]
                    if x is None:
                        continue
                    if aid == "ifrs-full_CashAndCashEquivalents" or nm == "현금및현금성자산":
                        sums["cash"][k] += x; seen_cash = True
                    elif "단기금융상품" in nm or nm == "단기금융자산":
                        sums["cash"][k] += x
                    elif ("차입금" in nm or ("사채" in nm and "이자" not in nm and "할인" not in nm)) and "리스" not in nm and "합계" not in nm:
                        sums["debt"][k] += x
                        if "단기" in nm or "유동성" in nm:
                            sums["st_debt"][k] += x
            elif sj == "CF":
                if aid == "ifrs-full_CashFlowsFromUsedInOperatingActivities" or (nm.startswith("영업활동") and "현금흐름" in nm):
                    put("ocf", v)
                elif aid == "ifrs-full_PurchaseOfPropertyPlantAndEquipment" or "유형자산의취득" in nm:
                    put("capex", [abs(x) if x is not None else None for x in v])
                elif aid == "ifrs-full_DividendsPaidClassifiedAsFinancingActivities" or ("배당금" in nm and "지급" in nm and "수취" not in nm):
                    put("divpaid", [abs(x) if x is not None else None for x in v])
        o["cash"] = sums["cash"] if seen_cash else None
        o["debt"], o["st_debt"] = sums["debt"], sums["st_debt"]
        out = o
        break
    cache[key] = out
    return out


# ───────────────────────── 배당 (배당가치 노트와 같은 방식) ─────────────────────────
def dividends(corp, year, cache):
    key = f"{corp}_{year}"
    if key in cache:
        return cache[key]
    j = dart("alotMatter.json", {"corp_code": corp, "bsns_year": str(year), "reprt_code": "11011"})
    out = {"dps": [None] * 3, "payout": [None] * 3}
    if j:
        for it in j.get("list", []):
            se = (it.get("se") or "").replace(" ", "")
            kind = (it.get("stock_knd") or "").strip()
            vals = [num(it.get("thstrm")), num(it.get("frmtrm")), num(it.get("lwfr"))]
            if "주당현금배당금" in se and kind in ("", "보통주"):
                out["dps"] = vals
            elif "현금배당성향" in se:
                out["payout"] = vals
    cache[key] = out
    return out


# ───────────────────────── 점수 계산 도구 ─────────────────────────
def g0(d, k, i=0):
    v = (d or {}).get(k)
    return v[i] if v and len(v) > i else None


def ratio(a, b):
    return a / b if a is not None and b not in (None, 0) else None


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def item(t, p, v, why=""):
    """판정 항목: t 이름, p 점수(1·0.5·0·None=자료 없음), v 표시 값, why 기준"""
    return {"t": t, "p": p, "v": v, "c": why}


def fmt_eok(x):
    return None if x is None else f"{x / 1e8:,.0f}억"


def pct(x, d=1):
    return "-" if x is None else f"{x * 100:.{d}f}%"


PKEYS = ("magic", "graham", "fscore", "growth", "trend", "dividend")


T0 = time.time()


def main():
    if not DART_KEY:
        sys.exit("[중단] DART_API_KEY가 없습니다. 저장소 Settings → Secrets에 OpenDART 인증키를 등록하세요.")
    Y = NOW.year - 1 if NOW.month >= 4 else NOW.year - 2
    years = list(range(Y - 5, Y + 1))
    log(f"기준 사업연도 {Y} (분석 구간 {years[0]}~{Y})")
    W = CFG["weights"]
    GR = CFG["graham"]

    # 1) 주가·관리종목
    bas_dt, rows = fetch_prices()
    markets = {"KOSPI", "KOSDAQ"} if CFG.get("include_kosdaq", True) else {"KOSPI"}
    prices = {}
    for it in rows:
        if it.get("mrktCtg") not in ("KOSPI", "KOSDAQ"):
            continue
        code = it.get("srtnCd", "").lstrip("A")
        pr, mv = num(it.get("clpr")), num(it.get("mrktTotAmt"))
        prices[code] = [it.get("itmsNm"), pr, r1((mv or 0) / 1e8, 0), it.get("mrktCtg")]
    write_json(DATA / "prices.json", {"date": bas_dt, "p": prices})
    flagged = flagged_naver()
    log(f"관리·경고·정지 종목: {len(flagged)}")

    min_mcap = CFG["min_mcap_eok"]
    uni = {c: v for c, v in prices.items()
           if v[3] in markets and c.endswith("0") and "스팩" not in (v[0] or "") and (v[2] or 0) >= min_mcap and v[1]}
    cmap = corp_codes()
    uni = {c: v for c, v in uni.items() if c in cmap}
    log(f"유니버스(보통주·시총 {min_mcap:,}억 이상): {len(uni)}종목")

    # 2) 업종 → 금융업 제외
    ccache = load_cache("company")
    for i, code in enumerate(uni, 1):
        company(cmap[code], ccache)
        if i % 100 == 0:
            save_cache("company", ccache); log(f"  업종 {i}/{len(uni)}")
    save_cache("company", ccache)
    excl = set(CFG.get("exclude_sectors", []))
    universe_out = {}
    for code, v in list(uni.items()):
        ks = (ccache.get(cmap[code]) or {}).get("ksic", "")[:2]
        if ks in excl:
            universe_out[code] = {"n": v[0], "m": v[3], "s": SECTORS.get(ks, "기타"), "f": ["금융업(마법공식 대상 아님)"]}
            del uni[code]
    corps = [cmap[c] for c in uni]

    # 3) 주요계정 6개년 + 최근 분기 누적
    fa = multi_accounts(corps, Y, "11011")
    fb = multi_accounts(corps, Y - 3, "11011")
    qinfo, qd = None, {}
    for (yy, rc, lb, n_, _) in quarter_candidates():
        d = multi_accounts(corps, yy, rc)
        if sum(1 for c in corps if (d.get(c) or {}).get("op")) >= len(corps) * 0.3:
            qinfo, qd = {"year": yy, "label": lb.replace("보고서", ""), "n": n_}, d
            break
    log(f"최근 분기: {qinfo['year']}년 {qinfo['label']}" if qinfo else "최근 분기 자료 없음 — 연간 기준만 사용")

    # 4) 1차 거르기 (망할 회사·거래 안 되는 회사)
    stage1 = []
    for code, (name, price, mcap, mkt) in uni.items():
        corp = cmap[code]
        a = fa.get(corp) or {}
        op = series(a, fb.get(corp), "op")
        ni = series(a, fb.get(corp), "ni")
        rev = series(a, fb.get(corp), "rev")
        eq = series(a, fb.get(corp), "equity")
        q = qd.get(corp) or {}
        same_fs = q.get("fs") == a.get("fs")
        def ttm(k, annual):
            v = q.get(k)
            if not same_fs or not v or annual is None or v[0] is None or v[1] is None:
                return None
            return annual + v[0] - v[1]
        op_ttm, ni_ttm, rev_ttm = ttm("op", op[-1]), ttm("ni", ni[-1]), ttm("rev", rev[-1])
        eq_now = g0(q, "equity") if same_fs and g0(q, "equity") else eq[-1]
        ks = (ccache.get(corp) or {}).get("ksic", "")[:2]
        fails = []
        op_now = op_ttm if op_ttm is not None else op[-1]
        if op[-1] is None:
            fails.append("재무 데이터 부족")
        elif op_now is None or op_now <= 0:
            fails.append("최근 영업적자")
        if eq_now is not None and eq_now <= 0:
            fails.append("자본잠식")
        if sum(1 for v in op[-3:] if v is not None and v <= 0) >= 2:
            fails.append("최근 3년 중 2년 이상 영업적자")
        if code in flagged:
            fails.append(flagged[code])
        universe_out[code] = {"n": name, "m": mkt, "s": SECTORS.get(ks, "기타"), "f": fails}
        if fails:
            continue
        qv = lambda k: (q.get(k) or [None, None]) if same_fs else [None, None]
        stage1.append(dict(code=code, corp=corp, name=name, market=mkt, price=price, mcap=mcap, sector=ks,
                           sector_name=SECTORS.get(ks, "기타"), rev=rev, op=op, ni=ni, eq=eq,
                           op_ttm=op_ttm, ni_ttm=ni_ttm, rev_ttm=rev_ttm, eq_now=eq_now,
                           q_op=qv("op")[:2], q_ni=qv("ni")[:2], q_rev=qv("rev")[:2],
                           q_bs={k: qv(k)[0] for k in ("assets", "liab", "equity")}))
    log(f"1차(영업흑자·자본잠식·관리종목) 통과: {len(stage1)}종목")

    # 5) 일봉 → 거래대금·추세
    stage2 = []
    for i, rec in enumerate(stage1, 1):
        t = fchart(rec["code"], 260)
        time.sleep(0.05)
        c, v = t["c"], t["v"]
        tv = (sum(a * b for a, b in zip(c[-20:], v[-20:])) / len(c[-20:]) / 1e8) if c else None
        rec["tv"] = tv
        if c:
            hi = max(c[-250:])
            rec["hi52"] = c[-1] / hi
            rec["ma200"] = (c[-1] >= sum(c[-200:]) / 200) if len(c) >= 200 else None
            rec["r121"] = (c[-21] / c[-252] - 1) if len(c) >= 252 else ((c[-21] / c[0] - 1) if len(c) >= 130 else None)
            rec["r1m"] = c[-1] / c[-21] - 1 if len(c) >= 21 else None
            rec["spark"] = [round(x) for x in c[-250::5]]
        else:
            rec.update(hi52=None, ma200=None, r121=None, r1m=None, spark=[])
        if tv is not None and tv < CFG["min_trading_value_eok"]:
            universe_out[rec["code"]]["f"].append(f"거래대금 20일 평균 {tv:.1f}억")
            continue
        stage2.append(rec)
        if i % 100 == 0:
            log(f"  일봉 {i}/{len(stage1)}")
    log(f"2차(거래대금) 통과: {len(stage2)}종목")

    # 6) 전체 재무제표 (현금흐름·유동자산·유형자산·매출총이익·자본금·배당금 지급)
    fcache = load_cache("fsx")
    for i, rec in enumerate(stage2, 1):
        rec["fx"] = fsx(rec["corp"], Y, fcache) or {}
        if i % 50 == 0:
            save_cache("fsx", fcache); log(f"  재무제표 {i}/{len(stage2)} (DART {CALLS['dart']}회)")
    save_cache("fsx", fcache)
    dcache = load_cache("div")
    for i, rec in enumerate(stage2, 1):
        da, db = dividends(rec["corp"], Y, dcache), dividends(rec["corp"], Y - 3, dcache)
        rec["dps6"] = [db["dps"][2], db["dps"][1], db["dps"][0], da["dps"][2], da["dps"][1], da["dps"][0]]
        rec["payout"] = da["payout"][0]
        if i % 100 == 0:
            save_cache("div", dcache); log(f"  배당 {i}/{len(stage2)}")
    save_cache("div", dcache)

    # 7) 지표 계산
    for rec in stage2:
        fx = rec["fx"]
        mcap_won = rec["mcap"] * 1e8
        ebit = rec["op_ttm"] if rec["op_ttm"] is not None else rec["op"][-1]
        ni_now = rec["ni_ttm"] if rec["ni_ttm"] is not None else rec["ni"][-1]
        ctrl_share = 1.0
        if g0(fx, "ctrl_ni") and g0(fx, "ni") and g0(fx, "ni") > 0:
            ctrl_share = clamp(g0(fx, "ctrl_ni") / g0(fx, "ni"), 0.3, 1.2)
        ni_ctrl = ni_now * ctrl_share if ni_now is not None else None
        eq_ctrl = (rec["eq_now"] * (g0(fx, "ctrl_eq") / g0(fx, "equity"))) if rec["eq_now"] and g0(fx, "ctrl_eq") and g0(fx, "equity") else rec["eq_now"]
        cash = g0(fx, "cash") or 0.0
        debt = g0(fx, "debt") or 0.0
        ev = mcap_won + debt - cash
        rec["ev"] = ev
        rec["ey"] = (ebit / ev) if ev > mcap_won * 0.05 else 1.0  # 현금이 시총보다 많으면 최고 수준으로
        ca, cl, ppe = g0(fx, "ca"), g0(fx, "cl"), g0(fx, "ppe") or 0.0
        if ca is not None and cl is not None:
            nwc = max((ca - (g0(fx, "cash") or 0.0)) - (cl - (g0(fx, "st_debt") or 0.0)), 0.0)
            capital = max(nwc + ppe, (g0(fx, "assets") or 0) * 0.05, 1.0)
            rec["roc"] = ebit / capital
        else:
            rec["roc"] = None
        rec["per"] = mcap_won / ni_ctrl if ni_ctrl and ni_ctrl > 0 else None
        rec["pbr"] = mcap_won / eq_ctrl if eq_ctrl and eq_ctrl > 0 else None
        rec["cr"] = ratio(ca, cl)
        rec["net_debt"] = debt - cash
        rec["debt_ratio"] = ratio(g0(fx, "liab"), g0(fx, "equity"))
        rec["roe"] = ni_ctrl / eq_ctrl if ni_ctrl is not None and eq_ctrl and eq_ctrl > 0 else None
        rev_now = rec["rev_ttm"] if rec["rev_ttm"] is not None else rec["rev"][-1]
        rec["opm"] = ebit / rev_now if rev_now and rev_now > 0 else None
        rec["eps"] = ni_ctrl / (mcap_won / rec["price"]) if ni_ctrl is not None and rec["price"] else None
        rec["bps"] = eq_ctrl / (mcap_won / rec["price"]) if eq_ctrl and rec["price"] else None
        dp = g0(fx, "divpaid")
        rec["dy"] = dp / mcap_won if dp else 0.0
        d6 = rec["dps6"]
        if d6[-1] and rec["price"]:
            rec["dy"] = d6[-1] / rec["price"]
        consec = 0
        for x in reversed(d6):
            if x and x > 0:
                consec += 1
            else:
                break
        rec["consec"] = consec
        rec["cuts"] = sum(1 for k in range(2, 6) if d6[k] is not None and d6[k - 1] and d6[k] < d6[k - 1] * 0.97)
        rec["dps_g"] = cagr(d6[2], d6[5], 3)

        # ── 그레이엄 안전성 ──
        ni6 = rec["ni"]
        pos = sum(1 for x in ni6 if x is not None and x > 0)
        gi = []
        gi.append(item("이익 안정성", 1 if pos == 6 else 0.5 if pos == 5 else 0, f"6년 중 {pos}년 흑자", "6년 연속 순이익 흑자"))
        dps = [g0(fx, "divpaid", 0), g0(fx, "divpaid", 1)]
        nd = sum(1 for x in dps if x and x > 0)
        gi.append(item("배당 지속", 1 if nd == 2 else 0.5 if nd == 1 else 0, f"최근 2년 중 {nd}년 배당", "최근 2년 연속 현금배당"))
        a_old, a_new = mean(ni6[0:3]), mean(ni6[3:6])
        if a_old and a_old > 0 and a_new is not None:
            gr = a_new / a_old - 1
            gi.append(item("이익 성장", 1 if gr >= GR["min_profit_growth"] / 100 else 0.5 if gr >= 0 else 0, f"3년 평균 {pct(gr, 0)}", f"최근 3년 평균 순이익이 직전 3년보다 {GR['min_profit_growth']}% 이상"))
        else:
            gi.append(item("이익 성장", 0, "비교 불가(과거 적자)", f"최근 3년 평균 순이익이 직전 3년보다 {GR['min_profit_growth']}% 이상"))
        cr = rec["cr"]
        gi.append(item("유동비율", None if cr is None else 1 if cr >= GR["min_current_ratio"] else 0.5 if cr >= 1.5 else 0,
                       "-" if cr is None else f"{cr * 100:.0f}%", f"유동자산 ÷ 유동부채 {GR['min_current_ratio'] * 100:.0f}% 이상"))
        if ca is not None and cl is not None:
            ok = debt <= ca - cl
            dr = rec["debt_ratio"]
            gi.append(item("재무 안정", 1 if ok else 0.5 if dr is not None and dr <= 1 else 0,
                           f"차입금 {fmt_eok(debt)} / 순운전자본 {fmt_eok(ca - cl)}" + (f", 부채비율 {dr * 100:.0f}%" if dr is not None else ""),
                           "차입금 < 순운전자본 (부채비율 100% 이하면 △)"))
        else:
            gi.append(item("재무 안정", None, "-", "차입금이 순운전자본보다 적을 것"))
        per, pbr = rec["per"], rec["pbr"]
        gi.append(item("PER", None if per is None else 1 if per <= GR["max_per"] else 0.5 if per <= GR["max_per"] * 4 / 3 else 0,
                       "적자" if per is None else f"{per:.1f}배", f"PER {GR['max_per']}배 이하"))
        if pbr is not None:
            ok = pbr <= GR["max_pbr"] or (per is not None and per * pbr <= GR["max_per_x_pbr"])
            gi.append(item("PBR", 1 if ok else 0.5 if pbr <= GR["max_pbr"] * 5 / 3 else 0,
                           f"{pbr:.2f}배" + (f" (PER×PBR {per * pbr:.1f})" if per else ""), f"PBR {GR['max_pbr']}배 이하 또는 PER×PBR {GR['max_per_x_pbr']} 이하"))
        else:
            gi.append(item("PBR", None, "-", f"PBR {GR['max_pbr']}배 이하"))
        av = [x["p"] for x in gi if x["p"] is not None]
        rec["graham"] = gi
        rec["s_graham"] = sum(av) / len(gi) * 100 if av else 0

        # ── 피오트로스키 F-스코어 (연간: 기준연도 vs 전년) ──
        def v2(k):
            return g0(fx, k, 0), g0(fx, k, 1)
        ni0, ni1 = v2("ctrl_ni") if g0(fx, "ctrl_ni") is not None else v2("ni")
        as0, as1 = v2("assets")
        ocf0, _ = v2("ocf")
        fi = []
        roa0, roa1 = ratio(ni0, as0), ratio(ni1, as1)
        fi.append(item("ROA 흑자", None if roa0 is None else int(roa0 > 0), pct(roa0), "총자산이익률 > 0"))
        fi.append(item("영업현금흐름 흑자", None if ocf0 is None else int(ocf0 > 0), fmt_eok(ocf0) or "-", "영업활동현금흐름 > 0"))
        fi.append(item("ROA 개선", None if roa0 is None or roa1 is None else int(roa0 > roa1), f"{pct(roa1)} → {pct(roa0)}", "ROA가 전년보다 높음"))
        fi.append(item("이익의 질", None if ocf0 is None or ni0 is None else int(ocf0 > ni0), f"영업CF {fmt_eok(ocf0) or '-'} / 순이익 {fmt_eok(ni0) or '-'}", "영업현금흐름 > 순이익"))
        lv0, lv1 = ratio(g0(fx, "ncl", 0), as0), ratio(g0(fx, "ncl", 1), as1)
        fi.append(item("장기부채 감소", None if lv0 is None or lv1 is None else int(lv0 <= lv1), f"{pct(lv1)} → {pct(lv0)}", "비유동부채 ÷ 총자산이 줄거나 같음"))
        c0, c1 = ratio(g0(fx, "ca", 0), g0(fx, "cl", 0)), ratio(g0(fx, "ca", 1), g0(fx, "cl", 1))
        fi.append(item("유동비율 개선", None if c0 is None or c1 is None else int(c0 > c1), "-" if c0 is None or c1 is None else f"{c1 * 100:.0f}% → {c0 * 100:.0f}%", "유동비율이 전년보다 높음"))
        k0, k1 = v2("cap")
        fi.append(item("신주 발행 없음", None if k0 is None or k1 is None else int(k0 <= k1 * 1.001), "-" if k0 is None or k1 is None else ("자본금 변동 없음" if k0 <= k1 * 1.001 else f"자본금 {fmt_eok(k1)} → {fmt_eok(k0)}"), "자본금이 늘지 않음"))
        gm0, gm1 = ratio(g0(fx, "gp", 0), g0(fx, "rev", 0)), ratio(g0(fx, "gp", 1), g0(fx, "rev", 1))
        fi.append(item("매출총이익률 개선", None if gm0 is None or gm1 is None else int(gm0 > gm1), f"{pct(gm1)} → {pct(gm0)}" if gm0 is not None and gm1 is not None else "매출총이익 미공시", "매출총이익률이 전년보다 높음"))
        at0, at1 = ratio(g0(fx, "rev", 0), as0), ratio(g0(fx, "rev", 1), as1)
        fi.append(item("자산회전율 개선", None if at0 is None or at1 is None else int(at0 > at1), "-" if at0 is None or at1 is None else f"{at1:.2f} → {at0:.2f}회", "매출 ÷ 총자산이 전년보다 높음"))
        got = [x["p"] for x in fi if x["p"] is not None]
        F = round(sum(got) / len(got) * 9) if len(got) >= 6 else None
        rec["fitems"], rec["F"] = fi, F
        rec["s_fscore"] = F / 9 * 100 if F is not None else 50

        # ── 린치 PEG·성장 구분 ──
        g_hist = cagr(ni6[2], ni6[5], 3)
        qn = rec["q_ni"]
        g_rec = None
        if qn[1] and qn[1] > 0 and qn[0] is not None:
            g_rec = qn[0] / qn[1] - 1
        qo = rec["q_op"]
        op_yoy = qo[0] / qo[1] - 1 if qo[1] and qo[1] > 0 and qo[0] is not None else None
        gs = [x for x in (g_hist, g_rec) if x is not None]
        g = clamp(mean(gs), -0.5, 1.0) if gs else None
        rev_g = cagr(rec["rev"][2], rec["rev"][5], 3)
        peg = per / (g * 100) if per and g and g > 0 else None
        P = CFG["peg"]
        rec["s_growth"] = 0 if peg is None else clamp((P["worst"] - peg) / (P["worst"] - P["best"])) * 100
        if g is not None and g >= 0.10 and (op_yoy is None or op_yoy >= 0):
            tag = "성장"
        elif (g is not None and g < 0) or (op_yoy is not None and op_yoy < -0.10):
            tag = "하향"
        else:
            tag = "정체"
        rec["growth"] = {"g_hist": g_hist, "g_rec": g_rec, "g": g, "op_yoy": op_yoy, "rev_g": rev_g, "peg": peg, "tag": tag}

    # 상대 순위가 필요한 항목: 마법공식·추세
    n = len(stage2)
    p_ey = pct_rank([r["ey"] for r in stage2])
    p_roc = pct_rank([r["roc"] for r in stage2])
    p_mom = pct_rank([r.get("r121") for r in stage2])
    p_dy = pct_rank([r["dy"] if r["dy"] else None for r in stage2])
    ord_ey = {id(r): k for k, r in enumerate(sorted(stage2, key=lambda r: -(r["ey"] if r["ey"] is not None else -9)), 1)}
    ord_roc = {id(r): k for k, r in enumerate(sorted(stage2, key=lambda r: -(r["roc"] if r["roc"] is not None else -9)), 1)}
    mrank = sorted(stage2, key=lambda r: ord_ey[id(r)] + ord_roc[id(r)])
    for k, r in enumerate(mrank, 1):
        r["magic_rank"] = k
    TR = CFG["trend"]
    DB = CFG["dividend_bonus"]
    for i, rec in enumerate(stage2):
        rec["s_magic"] = (p_ey[i] + p_roc[i]) / 2 * 100
        if rec["dy"] and rec["dy"] > 0:
            dg = rec["dps_g"]
            rec["s_dividend"] = p_dy[i] * 50 + rec["consec"] / 6 * 25 + (25 if dg is not None and dg > 0 else 12.5 if dg is not None and dg >= -0.02 else 0) - rec["cuts"] * 5
            rec["s_dividend"] = clamp(rec["s_dividend"], 0, 100)
        else:
            rec["s_dividend"] = 0.0
        ti = []
        ti.append(item("12-1개월 수익률", round(p_mom[i], 2) if rec.get("r121") is not None else None, pct(rec.get("r121"), 0),
                       "최근 1개월을 뺀 1년 수익률 (상위일수록 높은 점수)"))
        ti.append(item("200일선 위", None if rec.get("ma200") is None else int(rec["ma200"]), "-" if rec.get("ma200") is None else ("위" if rec["ma200"] else "아래"), "현재가가 200일 이동평균 위"))
        h = rec.get("hi52")
        ti.append(item("52주 고가 근접", None if h is None else 1 if h >= TR["near_high"] / 100 else 0.5 if h >= 0.7 else 0,
                       "-" if h is None else f"고가 대비 {h * 100:.0f}%", f"52주 최고가의 {TR['near_high']}% 이상"))
        wts = [50, 25, 25]
        rec["s_trend"] = sum((x["p"] if x["p"] is not None else 0.5) * w for x, w in zip(ti, wts))
        rec["titems"] = ti
        dyp = rec["dy"] * 100
        rec["bonus"] = DB["points"] if dyp >= DB["yield_full"] else DB["points"] / 2 if dyp >= DB["yield_half"] else 0
        rec["score"] = min(100.0, sum(W.get(k, 0) * rec[f"s_{k}"] for k in PKEYS) / sum(W.values()) + rec["bonus"])
        rec["penalty"], rec["risk"] = 0, []

    # 8) 위험 공시 확인 (최근 3년) — 종목별로 30일마다 나눠서 갱신, 하루 갱신 수·시간 상한
    stage2.sort(key=lambda r: -r["score"])
    raw = load_cache("risk")
    rcache = {}
    for k, v in raw.items():  # 예전 형식(법인코드_연월) 변환
        if isinstance(v, dict) and "d" in v:
            rcache[k] = v
        elif "_" in k:
            rcache[k.split("_")[0]] = {"d": k.split("_")[1] + "01", "r": v}
    end = NOW.strftime("%Y%m%d"); bgn = (NOW - dt.timedelta(days=365 * 3)).strftime("%Y%m%d")
    stale = (NOW - dt.timedelta(days=CFG.get("risk_refresh_days", 30))).strftime("%Y%m%d")
    targets = stage2[:CFG["risk_check_top"] or len(stage2)]
    todo = [r for r in targets if r["corp"] not in rcache or rcache[r["corp"]]["d"] < stale]
    todo.sort(key=lambda r: (r["corp"] in rcache, -r["score"]))  # 처음 보는 종목·상위 종목 먼저
    limit = CFG.get("risk_max_per_run", 250)
    done = 0
    for rec in todo:
        if done >= limit or time.time() - T0 > CFG.get("time_budget_min", 100) * 60:
            log(f"  위험 공시: 오늘은 {done}종목만 갱신 (나머지 {len(todo) - done}종목은 다음 실행에서)")
            break
        rcache[rec["corp"]] = {"d": end, "r": risk_history(rec["corp"], bgn, end)}
        done += 1
        if done % 50 == 0:
            save_cache("risk", rcache); log(f"  위험 공시 {done}/{min(limit, len(todo))}")
    for rec in targets:
        rec["risk"] = (rcache.get(rec["corp"]) or {}).get("r", [])
        if rec["risk"]:
            rec["penalty"] = CFG["risk_penalty"]
            rec["score"] = max(0.0, rec["score"] - rec["penalty"])
    save_cache("risk", rcache)
    stage2.sort(key=lambda r: -r["score"])
    for k, rec in enumerate(stage2, 1):
        rec["rank"] = k

    # 9) 설명 문장
    LBL = {"magic": "마법공식", "graham": "그레이엄 안전성", "fscore": "F-스코어", "growth": "성장(PEG)", "trend": "추세", "dividend": "배당"}
    for rec in stage2:
        why, warn = [], []
        if rec["magic_rank"] <= max(10, n // 10):
            why.append(f"마법공식 {rec['magic_rank']}위/{n} — 이익수익률 {pct(rec['ey'])}, 자본수익률 {pct(rec['roc'], 0)}")
        if rec["s_graham"] >= 75:
            why.append(f"그레이엄 안전성 {rec['s_graham']:.0f}점 — " + ", ".join(x["t"] for x in rec["graham"] if x["p"] == 1)[:60])
        if rec["F"] is not None and rec["F"] >= 7:
            why.append(f"F-스코어 {rec['F']}/9 — 재무가 개선되는 중")
        gr = rec["growth"]
        if gr["peg"] is not None and gr["peg"] <= 1:
            why.append(f"PEG {gr['peg']:.2f} — 이익 성장 대비 싼 편 (성장률 {pct(gr['g'], 0)})")
        if rec["s_trend"] >= 75:
            why.append("주가 추세 양호 — 200일선 위, 52주 고가 근처")
        if rec["dy"] * 100 >= DB["yield_half"]:
            why.append(f"배당수익률 약 {rec['dy'] * 100:.1f}%, {rec['consec']}년 연속 배당" + (f" (가점 +{rec['bonus']:g})" if rec["bonus"] else ""))
        if not why:
            best = max((k for k in PKEYS if W.get(k)), key=lambda k: rec[f"s_{k}"])
            why.append(f"{LBL[best]} 점수가 가장 높음 ({rec[f's_{best}']:.0f}점)")
        if rec["risk"]:
            warn.append("최근 3년 위험 공시: " + ", ".join(x["t"] for x in rec["risk"]) + f" (−{rec['penalty']}점)")
        if rec["sector"] in CYCLICAL and rec["per"] and rec["per"] < 8:
            warn.append("경기민감 업종 — PER이 낮을 때가 실적 고점일 수 있음 (린치)")
        if gr["tag"] == "하향":
            warn.append("이익 하향 추세" + (f" — 최근 누적 영업이익 전년 대비 {pct(gr['op_yoy'], 0)}" if gr["op_yoy"] is not None else ""))
        if rec["ni"][-1] and rec["op"][-1] and rec["op"][-1] > 0 and rec["ni"][-1] > rec["op"][-1] * 1.5:
            warn.append("순이익이 영업이익보다 크게 많음 — 일회성 이익 여부 확인")
        if rec["F"] is not None and rec["F"] <= 3:
            warn.append(f"F-스코어 {rec['F']}/9 — 재무 악화 신호")
        if rec.get("ma200") is False and (rec.get("hi52") or 1) < 0.7:
            warn.append("하락 추세 — 바닥 확인 후 분할 매수 권장")
        if rec["tv"] is not None and rec["tv"] < 5:
            warn.append(f"거래 적음 (20일 평균 {rec['tv']:.1f}억)")
        if "지주" in (rec["name"] or "") or rec["name"].endswith("홀딩스"):
            warn.append("지주회사 — 자회사 이익이 섞여 있어 마법공식 수치가 부풀 수 있음")
        rec["why"], rec["warn"] = why, warn

    # 10) 저장
    def brief(r):
        return {"n": r["name"], "m": r["market"], "s": r["sector_name"], "sc": r1(r["score"]), "rk": r["rank"],
                "p": [r1(r[f"s_{k}"], 0) for k in PKEYS],
                "F": r["F"], "per": r1(r["per"]), "pbr": r1(r["pbr"], 2), "tag": r["growth"]["tag"],
                "ma": r.get("ma200"), "h52": r1((r.get("hi52") or 0) * 100, 0) if r.get("hi52") else None,
                "c": r["price"], "mc": r["mcap"], "dy": r1(r["dy"] * 100, 2), "dc": r["consec"], "cut": r["cuts"],
                "roe": r1(r["roe"] * 100) if r["roe"] is not None else None, "opm": r1(r["opm"] * 100) if r["opm"] is not None else None,
                "po": r1(r["payout"], 0), "bon": r["bonus"], "pen": r["penalty"], "w": r["why"][0] if r["why"] else "",
                "nw": len(r["warn"]), "f": [], "risk": bool(r["risk"])}

    def qblock(r):
        """최근 분기 누적 실적: [당기 누적, 전년 동기 누적] (억원)"""
        if not qinfo or r["q_op"][0] is None:
            return None
        e = lambda v: [r1(x / 1e8, 0) if x is not None else None for x in v]
        bs = r.get("q_bs") or {}
        return {"lab": f"'{str(qinfo['year'])[2:]}.{qinfo['n']}Q", "rev": e(r["q_rev"]), "op": e(r["q_op"]), "ni": e(r["q_ni"]),
                "debt": r1(bs["liab"] / bs["equity"] * 100, 0) if bs.get("liab") is not None and bs.get("equity") else None}

    def full(r):
        gr = r["growth"]
        return {"code": r["code"], "name": r["name"], "market": r["market"], "sector": r["sector_name"],
                "price": r["price"], "mcap": r["mcap"], "score": r1(r["score"]), "rank": r["rank"],
                "parts": {k: r1(r[f"s_{k}"], 0) for k in PKEYS},
                "div": {"dps": [r1(x, 0) for x in r["dps6"]], "consec": r["consec"], "cuts": r["cuts"],
                        "payout": r1(r["payout"], 0), "g": r1(r["dps_g"] * 100) if r["dps_g"] is not None else None},
                "bonus": r["bonus"], "penalty": r["penalty"],
                "magic": {"ey": r1(r["ey"] * 100), "roc": r1((r["roc"] or 0) * 100), "rank": r["magic_rank"], "n": n,
                          "ebit": r1((r["op_ttm"] if r["op_ttm"] is not None else r["op"][-1]) / 1e8, 0), "ev": r1(r["ev"] / 1e8, 0),
                          "ttm": r["op_ttm"] is not None},
                "graham": r["graham"], "fitems": r["fitems"], "F": r["F"], "titems": r["titems"],
                "growth": {k: (r1(v * 100) if k not in ("peg", "tag") and v is not None else (r1(v, 2) if k == "peg" else v)) for k, v in gr.items()},
                "per": r1(r["per"]), "pbr": r1(r["pbr"], 2), "dy": r1(r["dy"] * 100, 2), "tv": r1(r["tv"]),
                "roe": r1(r["roe"] * 100) if r["roe"] is not None else None, "opm": r1(r["opm"] * 100) if r["opm"] is not None else None,
                "eps": r1(r["eps"], 0) if r["eps"] is not None else None, "bps": r1(r["bps"], 0) if r["bps"] is not None else None,
                "hi52": r1((r.get("hi52") or 0) * 100, 0) if r.get("hi52") else None, "ma200": r.get("ma200"),
                "r121": r1((r.get("r121") or 0) * 100) if r.get("r121") is not None else None,
                "spark": r.get("spark", []), "why": r["why"], "warn": r["warn"], "risk": r["risk"],
                "years": years, "rev": [r1(x / 1e8, 0) if x is not None else None for x in r["rev"]],
                "op": [r1(x / 1e8, 0) if x is not None else None for x in r["op"]],
                "ni": [r1(x / 1e8, 0) if x is not None else None for x in r["ni"]],
                "ttm": {"op": r1(r["op_ttm"] / 1e8, 0) if r["op_ttm"] is not None else None,
                        "ni": r1(r["ni_ttm"] / 1e8, 0) if r["ni_ttm"] is not None else None,
                        "rev": r1(r["rev_ttm"] / 1e8, 0) if r["rev_ttm"] is not None else None},
                "q": qblock(r)}

    for r in stage2:
        universe_out[r["code"]] = brief(r)
    top = stage2[:CFG["top_n"]]
    latest = {"updated": NOW.strftime("%Y-%m-%d %H:%M"), "price_date": bas_dt, "fiscal_year": Y,
              "quarter": qinfo, "weights": W, "config": CFG,
              "counts": {"universe": len(uni) + sum(1 for v in universe_out.values() if "금융업(마법공식 대상 아님)" in v.get("f", [])),
                         "stage1": len(stage1), "scored": n},
              "candidates": [full(r) for r in top],
              "others": [full(r) for r in stage2[CFG["top_n"]:CFG["top_n"] + CFG["others_n"]]]}
    write_json(DATA / "latest.json", latest)
    write_json(DATA / "universe.json", {"date": bas_dt, "n": n, "u": universe_out})
    write_json(DATA / "details.json", {r["code"]: full(r) for r in stage2})  # 조건 검색 결과의 상세 화면용

    # 업종 중앙값 (업종 비교용)
    def med(xs):
        xs = sorted(x for x in xs if x is not None)
        if not xs:
            return None
        k = len(xs) // 2
        return xs[k] if len(xs) % 2 else (xs[k - 1] + xs[k]) / 2
    groups = {}
    for r in stage2:
        groups.setdefault(r["sector_name"], []).append(r)
    sectors = {}
    for name, rs in groups.items():
        sectors[name] = {"n": len(rs),
                         "per": r1(med([r["per"] for r in rs if r["per"] and r["per"] < 100])),
                         "pbr": r1(med([r["pbr"] for r in rs]), 2),
                         "roe": r1(med([r["roe"] * 100 if r["roe"] is not None else None for r in rs])),
                         "opm": r1(med([r["opm"] * 100 if r["opm"] is not None else None for r in rs])),
                         "dy": r1(med([r["dy"] * 100 for r in rs]), 2),
                         "sc": r1(med([r["score"] for r in rs]))}
    write_json(DATA / "sectors.json", sectors)

    # 월별 스냅샷 (그 달 첫 수집 때만)
    HIST.mkdir(parents=True, exist_ok=True)
    m = NOW.strftime("%Y-%m")
    hf = HIST / f"{m}.json"
    if not hf.exists() and top:
        write_json(hf, {"date": bas_dt, "items": [{"code": r["code"], "name": r["name"], "price": r["price"], "score": r1(r["score"]), "rank": r["rank"]} for r in top]})
    idx = sorted([p.stem for p in HIST.glob("*.json") if p.stem != "index"], reverse=True)
    write_json(HIST / "index.json", idx)
    log(f"완료: 평가 {n}종목, 상위 {len(top)}종목 저장 (DART 호출 {CALLS['dart']}회)")


if __name__ == "__main__":
    main()
