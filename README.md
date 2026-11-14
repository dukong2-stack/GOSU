# 고수 노트

배당가치 노트를 복사해 만든 별도 앱입니다. 국내 상장사를 전문가들의 종목 선정 방식 다섯 가지로 채점해서
상위 후보를 보여주고, 보유 종목의 유지·교체·매도 신호를 매일 다시 판정합니다.

## 선정 흐름
1. **걸러내기**: 시총 1,000억 이상 보통주 중 금융업, 최근 영업적자, 자본잠식, 3년 중 2년 이상 적자,
   관리·경고·정지 종목, 하루 거래대금 1억 미만 제외
2. **채점** (비중은 config.json의 weights)
   - 마법공식(그린블라트) 30: 이익수익률 EBIT/EV + 자본수익률 EBIT/(순운전자본+유형자산), 최근 4분기 영업이익 사용
   - 그레이엄 안전성 30: 6년 연속 흑자, 2년 연속 배당, 이익 성장, 유동비율, 차입금<순운전자본, PER, PBR
   - F-스코어(피오트로스키) 20: 9개 항목을 전년과 비교
   - 성장(린치 PEG) 10: PER ÷ 이익 성장률, 성장·정체·하향 태그
   - 추세(오닐) 10: 12-1개월 수익률, 200일선, 52주 고가 근접
   - 배당 0: 배당수익률·연속 배당·배당 증가 (기본 0, 앱에서 올려 쓰기)
3. **가점·감점**: 배당수익률 가점(최대 +5), 최근 3년 위험 공시 −15 (평가 종목 전체 확인)
4. **조건 검색(앱)**: 후보 탭에서 프리셋(기본·배당주 위주·균형형·성장·모멘텀)을 고르거나
   비중 슬라이더와 조건(배당주만, 시장, 성장 구분, F-스코어, PER, PBR, 시총, 200일선, 바닥 신호, 위험 공시 제외)을
   조합하면 그 자리에서 다시 순위를 매김. 조건은 폰에 저장
5. **매수 시점**: 같은 계정의 바닥 신호 앱(badak) 결과를 읽어 단계 배지 표시

## 관심·상세 (collector/focus.py)
- 대상: watch.json의 관심·보유 종목코드 + 기본 순위 상위 30
- 종목별 data/focus/{코드}.json: 5년 재무·수익성·안정성·현금흐름·주당지표·연말 PER/PBR/배당률 표,
  적정주가 범위(그레이엄 수·PER/PBR/배당률 밴드), 최근 1년 공시(주요·위험 표시), 사업보고서 링크,
  애널리스트 리포트(네이버 리서치), 뉴스(네이버 금융)
- 알림: 관심·보유 종목의 바닥 신호 진입, 점수 ±10, 적정가 고평가/저평가 진입, 새 주요·위험 공시, 걸러내기 탈락
  → 앱 관심 탭 + Gmail (SMTP 시크릿이 있을 때)
- 앱에서 관심 목록을 바꾸면(토큰 동기화 시) focus 워크플로가 바로 실행되어 몇 분 안에 반영

## 구조
- `collector/collect.py` — GitHub Actions가 화~토 오전 7시 20분에 실행, 결과를 `data/`에 저장
- `config.json` — 기준 숫자와 비중. 숫자만 고치면 다음 수집부터 반영
- `index.html` — 앱 화면 (후보·조건 검색 / 관심 / 보유 / 기록 / 기준)
- `watch.json` — 관심·보유 종목코드 (앱이 GitHub 토큰으로 갱신, 수량·매입가·메모는 올리지 않음)
- `data/details.json` — 전 종목 상세 (조건 검색 결과를 누를 때만 불러옴)
- 보유 수량·매입가·메모는 폰 브라우저에만 저장 (기준 탭에서 엑셀 백업·복구)

## 처음 설정
1. GitHub에 새 저장소 `gosu` (Public) 만들고 이 폴더 안의 내용 전체 업로드 (.github, .nojekyll 포함)
2. Settings → Secrets and variables → Actions → New repository secret
   - `DART_API_KEY` = 배당가치 노트와 같은 OpenDART 키
   - `DATA_GO_KR_KEY` = (선택) 공공데이터포털 키. 없으면 네이버 시세로 수집
3. Settings → Actions → General → Workflow permissions → Read and write permissions 저장
4. Settings → Pages → Deploy from a branch → main / (root) 저장
5. Actions 탭 → collect → Run workflow (첫 실행은 캐시가 없어 40~80분)
6. https://dukong2-stack.github.io/gosu/ 를 폰 크롬에서 열고 홈 화면에 설치

## 관심 목록 동기화용 토큰 (선택)
1. GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token
2. Repository access: Only select repositories → gosu
3. Permissions → Repository permissions → Contents: Read and write
4. 만든 토큰(github_pat_…)을 앱 기준 탭 → 저장소 동기화에 붙여 넣기 (폰에만 저장됨)

## 알림 메일 (선택)
시세 메일(gpu-price)과 같은 이름으로 Secrets 등록: `SMTP_USER`, `SMTP_APP_PASSWORD`, `MAIL_TO`
