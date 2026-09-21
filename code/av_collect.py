# -*- coding: utf-8 -*-
"""세션 시점 시세 수집 — 계획(무엇을 받을지)과 병합(어떻게 넣을지)을 파일로 고정한다.

★ 왜 예약작업에서 세션으로 옮겼는가 (2026-09-05~06 실측)
  예약 실행은 성공 보고를 남기지만 그 세션이 쓴 파일은 이 워크스페이스에 도착하지 않는다.
  마커 왕복 시험으로 이틀 연속 확인했다(설계 문서 1-B절). 그 결과 `prices_av/`는 8월 28일에
  멈춰 있었고, 시트의 여력 숫자는 9일 묵은 종가를 분모로 쓰고 있었다.
  반면 세션 안에서 하는 일은 전부 정상이다. 그래서 수집을 세션 시점으로 내린다.
  대가는 "매일"이 "세션을 열 때마다"로 바뀌는 것이고, 얻는 것은 값이 실제로 도착한다는 것이다.

★ 왜 고정 슬롯(3일 순환)을 버리고 신선도 우선으로 바꿨는가
  `av_plan.json`의 슬롯은 **매일 도는 것**을 전제로 37종목을 3등분한 설계다. 세션이 불규칙하게
  열리면 이 전제가 깨진다 — 어제 받은 종목을 오늘 또 받고, 2주 묵은 종목은 슬롯이 안 돌아와
  계속 방치된다. 그래서 "오늘이 무슨 슬롯인가" 대신 **"지금 가장 오래된 것부터"** 받는다.
  이 규칙은 실행 주기가 어떻든 항상 옳고, 한도(하루 25회)를 가장 낡은 곳에 쓴다.

★ 역할 분담 — 왜 이 스크립트가 API를 직접 안 부르는가
  Alpha Vantage는 MCP 커넥터라 파이썬에서 못 부른다(세션만 부를 수 있다). 그래서
  **세션이 받아오고, 이 스크립트가 넣는다.** 넣는 쪽(병합)이 사고가 나는 자리이므로
  그 규칙만은 세션의 즉흥 판단이 아니라 파일로 남긴다 — 특히 "덮어쓰지 말고 병합",
  "행이 줄면 거부"는 일일 수집 프롬프트가 경고하던 바로 그 실수다.

사용법
    python3 av_collect.py --plan --budget 15        # 무엇을 받을지 (오래된 순)
    python3 av_collect.py --merge AAPL --csv-file /tmp/aapl.csv
    python3 av_collect.py --report                  # 커버리지·신선도 현황
"""
import argparse
import csv
import datetime
import glob
import io
import json
import os
import sys

BASE = '/home/claude'
AV_DIR = os.path.join(BASE, 'prices_av')
KEYS = os.path.join(BASE, 'valuation_models', 'price_keys.json')
AV_HEADER = ['timestamp', 'open', 'high', 'low', 'close', 'volume']

# Alpha Vantage 무료 플랜 실측 한도 (일일 수집 예약작업 프롬프트와 같은 값)
DAILY_LIMIT = 25


def today():
    return datetime.date.today()


def read_av_file(path):
    """기존 prices_av/<티커>.csv → {날짜: 행리스트}. '#' 주석 줄은 건너뛴다.
    데이터가 없는 껍데기 파일(수집 실패 기록만 있는 것)은 빈 dict로 돌려준다 —
    '파일이 있다'와 '값이 있다'는 다르다."""
    if not os.path.exists(path):
        return {}, None
    lines = [l for l in open(path, encoding='utf-8', errors='replace') if l.strip()]
    note = next((l.rstrip('\n') for l in lines if l.startswith('#')), None)
    body = [l for l in lines if not l.startswith('#')]
    if not body:
        return {}, note
    rows = list(csv.reader(io.StringIO(''.join(body))))
    if not rows or [c.strip() for c in rows[0]] != AV_HEADER:
        return {}, note
    out = {}
    for r in rows[1:]:
        if len(r) == len(AV_HEADER) and r[0].strip():
            out[r[0].strip()] = [c.strip() for c in r]
    return out, note


def parse_av_response(text):
    """Alpha Vantage TIME_SERIES_DAILY(datatype=csv) 응답 → {날짜: 행}.
    MCP가 JSON으로 감싸 주는 경우({"result": "...csv..."})도 벗겨 낸다."""
    text = text.strip()
    if text.startswith('{'):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            pass
        else:
            if 'error' in obj:
                raise SystemExit(f"  ✗ Alpha Vantage 오류 응답이다: {obj['error']}")
            for k in ('result', 'content', 'data', 'csv'):
                if isinstance(obj.get(k), str):
                    text = obj[k]
                    break
            else:
                raise SystemExit("  ✗ JSON인데 CSV 본문을 못 찾았다. 원본을 확인하라.")
    text = text.replace('\r\n', '\n').replace('\\r\\n', '\n').replace('\\n', '\n')
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise SystemExit("  ✗ 응답이 비었다.")
    head = [c.strip() for c in rows[0]]
    if head != AV_HEADER:
        raise SystemExit(f"  ✗ 헤더가 예상과 다르다.\n      기대: {AV_HEADER}\n      실제: {head}\n"
                         f"      (프리미엄 차단 응답이거나 outputsize=full을 쓴 것은 아닌지 확인하라)")
    out = {}
    for r in rows[1:]:
        if len(r) == len(AV_HEADER) and r[0].strip():
            out[r[0].strip()] = [c.strip() for c in r]
    if not out:
        raise SystemExit("  ✗ 데이터 행이 하나도 없다.")
    return out


# ★ 2026-09-11 — 벤치마크(SPY). 9aj층 「판정 성과표」가 "이 유니버스에 있을 가치가 있었나"의 참고
#   기준선으로 쓴다. 파일명을 _bench_ 접두로 두는 이유: s1_normalize.py가 prices_av/*.csv를 훑을
#   때 '_'로 시작하는 파일을 건너뛰므로, 벤치마크가 **채점 유니버스·prices.csv·팩터에 절대 섞이지
#   않는다.** (s0_prices_av_sync.py는 _bench_만 예외로 왕복시킨다.) price_keys.json의 '_벤치마크'
#   키가 있으면 그걸 쓰고, 없으면 SPY 하나.
def bench_targets():
    try:
        b = json.load(open(KEYS, encoding='utf-8')).get('_벤치마크') or {}
        return {t: f'_bench_{t}' for t in sorted(b) if not t.startswith('_')}
    except Exception:
        return {}


def av_path(ticker):
    """티커 → prices_av 파일 경로. 벤치마크는 _bench_ 접두 파일로 간다."""
    return os.path.join(AV_DIR, f'{bench_targets().get(ticker, ticker)}.csv')


def load_targets():
    """AV로 받을 수 있는 종목만 고른다.
    ★ 비USD 워크북(그룹A 14 + MUFG 도쿄 원주)은 제외한다 — 2026-09-05 실측으로 무료 플랜이
      한국거래소를 아예 커버하지 않고 도쿄 원주도 안 준다는 것을 확인했다. 미국 OTC 사본을
      대신 받으면 MUFG와 같은 통화 불일치를 새로 만드는 셈이라 받지 않는다."""
    keys = json.load(open(KEYS, encoding='utf-8'))['tickers']
    return sorted(t for t, v in keys.items()
                  if v.get('price_source') == 'prices' and v.get('price_cur') == 'USD')


LATEST_SRC = os.path.join(BASE, 'pipeline', 'out', 'prices_latest.csv')


def effective_latest():
    """티커별 '지금 화면에 쓰이는 최신 종가의 날짜'. 1층이 세 소스에서 뽑아 둔 파일을 읽는다.
    ★ prices_av만 보면 안 되는 이유: 33종목은 prices_av에 값이 없지만 prices_long(브라우저
      수집)에는 3~9일 된 값이 있다. prices_av 기준으로만 우선순위를 매기면 이미 값이 있는
      종목에 한도를 다 쓰고, 정작 화면에 나가는 값은 하나도 안 새로워진다."""
    out = {}
    if os.path.exists(LATEST_SRC):
        for r in csv.DictReader(open(LATEST_SRC, encoding='utf-8-sig')):
            out[r['ticker']] = r['date']
    return out


def staleness():
    """티커별 (경과일, 최신일, prices_av 행수).
    경과일은 **화면에 실제로 쓰이는 값** 기준이고, 행수는 이 스크립트가 채우는 자리의 현황이다."""
    eff = effective_latest()
    out = {}
    for t in load_targets() + sorted(bench_targets()):
        d, _ = read_av_file(av_path(t))
        n = len(d)
        # 벤치마크는 화면에 안 나가므로 prices_latest.csv에 없다 — 자기 파일 기준으로만 센다
        latest = eff.get(t) or (max(d) if d else None)
        if latest:
            age = (today() - datetime.date.fromisoformat(latest)).days
        else:
            age = 9999                   # 어느 소스에도 값이 없다 — 최우선
        out[t] = (age, latest, n)
    return out


def cmd_plan(budget):
    st = staleness()
    # ★ 벤치마크는 하루 1건이라 예산에 거의 부담이 없고, 빠지면 9aj층이 그날 벤치마크 없이 남는다 —
    #   경과 1일 이상이면 항상 맨 앞. (0일이면 오늘 이미 받은 것이라 건너뛴다.)
    bench = [t for t in sorted(bench_targets()) if t in st and st[t][0] >= 1]
    order = sorted(((t, v) for t, v in st.items() if t not in bench), key=lambda kv: (-kv[1][0], kv[0]))
    pick = [(t, st[t]) for t in bench] + order[:max(0, budget - len(bench))]
    print(f"■ 수집 계획 — 오래된 순 상위 {len(pick)}종목 (요청 예산 {budget}, 무료 플랜 일일 한도 {DAILY_LIMIT})")
    print(f"  대상 풀 {len(st)}종목(USD 상장분만) · 기준일 {today().isoformat()}\n")
    print(f"  {'티커':8s}{'현재 최신일':14s}{'경과':>6s}{'행수':>7s}")
    for t, (age, latest, n) in pick:
        age_s = '없음' if age == 9999 else f'{age}일'
        print(f"  {t:8s}{(latest or '—'):14s}{age_s:>6s}{n:>7d}")
    print("\n  다음 단계(세션에서): 위 티커마다")
    print("    TIME_SERIES_DAILY(symbol=<티커>, outputsize=compact, datatype=csv)")
    print("    → 응답 본문을 파일로 저장 → python3 av_collect.py --merge <티커> --csv-file <경로>")
    print("  ※ outputsize=full과 TIME_SERIES_DAILY_ADJUSTED는 무료 플랜에서 차단된다. compact만 쓴다.")
    return [t for t, _ in pick]


def cmd_merge(ticker, csv_file, dry_run=False):
    path = av_path(ticker)                   # 벤치마크(SPY)는 _bench_SPY.csv로 간다
    old, old_note = read_av_file(path)
    new = parse_av_response(open(csv_file, encoding='utf-8', errors='replace').read())

    merged = dict(old)
    merged.update(new)                       # 같은 날짜는 새 응답이 이긴다(정정 반영)
    n_old, n_new, n_mrg = len(old), len(new), len(merged)

    print(f"■ {ticker} 병합")
    print(f"  기존 {n_old}행" + (f" (껍데기: {old_note[:60]})" if not old and old_note else "")
          + f" · 응답 {n_new}행 · 병합 후 {n_mrg}행 (신규 {n_mrg - n_old}행)")

    # ★ 이 검사가 이 스크립트의 존재 이유다 — 병합 대신 덮어쓰면 표본이 100일에서 안 자란다.
    if n_mrg < n_old:
        raise SystemExit(f"  ✗ 행이 줄었다({n_old} → {n_mrg}). 병합이 아니라 덮어쓴 것이다. 거부한다.")
    if n_old and n_mrg == n_old and not (set(new) - set(old)):
        print("  ※ 새 날짜가 없다 — 이미 최신이거나 휴장 구간이다(정상).")

    latest = max(merged)
    age = (today() - datetime.date.fromisoformat(latest)).days
    print(f"  최신일 {latest} ({age}일 전)")

    if dry_run:
        print("  (dry-run — 저장하지 않음)")
        return

    os.makedirs(AV_DIR, exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(f"# SOURCE=Alpha Vantage TIME_SERIES_DAILY(compact) "
                f"MERGED_ON={today().isoformat()} N={n_mrg} "
                f"BY=세션 수집(av_collect.py) — 예약작업 산출물이 워크스페이스에 도착하지 않아 "
                f"2026-09-06부터 세션에서 수집한다\n")
        w = csv.writer(f)
        w.writerow(AV_HEADER)
        for d in sorted(merged, reverse=True):
            w.writerow(merged[d])
    print(f"  ✓ 저장: {os.path.relpath(path, BASE)}")


def cmd_report():
    """두 가지를 **따로** 보고한다 — 섞으면 착시가 생긴다.
    ① 실효 신선도: 화면(시트·대시보드)이 실제로 쓰는 최신 종가가 며칠 됐는가.
       세 소스를 합친 결과이며 prices_latest.csv 기준이다.
       ★ 이 값은 파이프라인(1층)을 다시 돌려야 갱신된다 — 방금 병합한 것이 여기 반영되려면
         `bash run_pipeline.sh`가 한 번 돌아야 한다.
    ② prices_av 커버리지: 세션 수집이 채우는 자리의 현황. 여기가 비어 있어도 다른 소스에
       값이 있으면 ①은 채워진다 — 그래서 둘을 한 숫자로 합치면 안 된다."""
    st = staleness()
    ages = sorted(v[0] for v in st.values() if v[0] != 9999)
    missing = [t for t, v in st.items() if v[0] == 9999]
    print(f"■ 시세 현황 — 대상 {len(st)}종목(USD 상장분), 기준 {today().isoformat()}\n")
    print("① 실효 신선도 (화면이 쓰는 값 · prices_latest.csv 기준)")
    if ages:
        med = ages[len(ages) // 2]
        print(f"   경과일 중앙값 {med}일 · 최소 {ages[0]}일 · 최대 {ages[-1]}일")
    if missing:
        print(f"   어느 소스에도 값 없음 {len(missing)}종목: {', '.join(missing)}")
    if not os.path.exists(LATEST_SRC):
        print("   ※ prices_latest.csv가 없다 — 파이프라인을 아직 안 돌렸다는 뜻(prices_av로 대체 계산).")
    buckets = {'0~2일': 0, '3~6일': 0, '7~13일': 0, '14일+': 0}
    for a in ages:
        k = '0~2일' if a <= 2 else '3~6일' if a <= 6 else '7~13일' if a <= 13 else '14일+'
        buckets[k] += 1
    print("   분포: " + " · ".join(f"{k} {v}종목" for k, v in buckets.items()))
    have_av = sum(1 for _, (_, _, n) in st.items() if n)
    print(f"\n② prices_av 커버리지 (세션 수집이 채우는 자리)")
    print(f"   값 보유 {have_av}/{len(st)}종목 · 미수집 {len(st) - have_av}종목")
    # 껍데기 파일 — '파일은 있는데 값이 없는' 상태를 따로 세어 둔다(커버리지 착시 방지)
    shells = []
    for p in sorted(glob.glob(os.path.join(AV_DIR, '*.csv'))):
        if os.path.basename(p).startswith('_'):
            continue
        d, note = read_av_file(p)
        if not d:
            shells.append(os.path.basename(p)[:-4])
    if shells:
        print(f"  ※ 껍데기 파일(수집 실패 기록만 있고 값 없음) {len(shells)}개: {', '.join(shells)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--plan', action='store_true')
    ap.add_argument('--budget', type=int, default=15)
    ap.add_argument('--merge', metavar='TICKER')
    ap.add_argument('--csv-file')
    ap.add_argument('--report', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    if a.plan:
        cmd_plan(a.budget)
    elif a.merge:
        if not a.csv_file:
            sys.exit("--merge에는 --csv-file이 필요하다")
        cmd_merge(a.merge, a.csv_file, a.dry_run)
    elif a.report:
        cmd_report()
    else:
        ap.print_help()


if __name__ == '__main__':
    main()


