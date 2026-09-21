# -*- coding: utf-8 -*-
"""prices_av/*.csv ↔ Drive 백업 파일 왕복 동기화 — 예약 트리거가 세션 컨테이너 경계를
넘어 시세 누적치를 이어받기 위한 다리.

★ 왜 필요한가 (2026-09-11 신설)
  av_collect.py의 docstring이 이미 실측으로 확인해 둔 사실: 예약 트리거는 매번 새
  컨테이너로 뜨고, 그 컨테이너가 쓴 파일은 다음 실행에도 이 워크스페이스에도 안 남는다.
  그래서 av_collect.py는 세션(사람이 채팅을 열 때)에서만 돌게 옮겨졌다. 하지만 그러면
  "매일 자동으로"는 원래부터 불가능해진다 — 세션은 사람이 열 때만 생긴다.
  이 스크립트는 컨테이너가 아니라 **Google Drive**를 누적치의 진짜 저장소로 삼아서,
  예약 트리거의 매 실행이 "어제까지 쌓인 값"을 이어받을 수 있게 한다.

★ 왜 sheet_export.csv와 다른 취급을 받는가 (BOM 사고 재발 방지)
  2026-09-04에 세션이 base64로 인코딩해 Drive에 직접 쓴 sheet_export.csv가 BOM이 깨져
  Code.gs의 정확 헤더 매칭(`head.indexOf('티커')`)을 못 찾은 사고가 있었다. 그래서
  sheet_export.csv는 지금도 사람이 "새 버전 업로드"로만 올린다.
  이 파일(prices_av_backup.csv)은 BOM이 없는 순수 영문 헤더(ticker,timestamp,...)이고
  Google Apps Script가 아니라 이 스크립트 자신만 다시 읽는다 — 그래도 "안 깨졌다고
  가정하지 않는다"는 원칙은 그대로 지킨다: --import가 구조 검증에 실패하면(헤더가
  다르거나 파싱이 안 되면) 그 파일을 신뢰하지 않고 명확히 실패한다(아무 것도 안 하는
  게 아니라 에러로 죽는다 — 조용히 깨진 채로 넘어가지 않는다).

★ 안전장치 — "행이 줄면 거부"가 아니라 "합집합이라 원래 못 줄어든다"
  --import은 `dict(old)`에 새 데이터를 `update()`하는 합집합 병합이라, 수학적으로
  merged 행수가 old보다 작아질 수 없다(cmd_merge와 같은 구조). 그래서 "행이 줄면
  거부"라는 조건은 애초에 발화하지 않는다 — 이건 cmd_merge 원본에도 있는 특성이고,
  이 스크립트가 새로 망가뜨린 게 아니다. 대신 실제로 뜻이 있는 두 안전장치를 건다:
  ① 백업 파일 구조(헤더)가 다르면 조용히 넘어가지 않고 SystemExit로 죽는다(빈 dict를
    반환해 "데이터가 원래 없었던 것"처럼 보이게 하지 않는다 — 이 파일은 사람이 안
    보는 내부용이라 sheet_export.csv보다 신뢰 전제가 낮다). ② 백업 안의 특정 티커
    행수가 로컬에 이미 있는 행수보다 뚜렷이 적으면(부분 손상 의심) 경고만 출력한다 —
    병합 자체는 안전하니 막지는 않지만, 사람이 알아채도록 남긴다.

사용법
    python3 s0_prices_av_sync.py --export /tmp/prices_av_backup.csv
    python3 s0_prices_av_sync.py --import /tmp/prices_av_backup.csv
    python3 s0_prices_av_sync.py --self-test
"""
import argparse
import csv
import datetime
import glob
import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import av_collect as AVC  # noqa: E402  (같은 디렉터리 — read_av_file/AV_HEADER 재사용)

BACKUP_HEADER = ['ticker', 'timestamp', 'open', 'high', 'low', 'close', 'volume']


def _av_dir(base):
    return os.path.join(base, 'prices_av')


# ★ 2026-09-19 — Drive 사본과 레포 사본이 갈라져 있었다
#   2026-09-12·15에 Drive 쪽 사본에만 들어간 기능(조각 내보내기 · `_watch_` 취급)이
#   레포로 돌아오지 않았고, 오늘 레포 쪽에만 넣은 안전장치(빈 백업·줄어듦 차단)는
#   Drive로 가지 않았다. 서로가 서로에게 없는 것을 갖고 있었다 — 어느 쪽도 상위집합이
#   아니라 **합쳐야** 했다. 이 파일이 그 합본이다.
#   같은 파일이 두 벌로 살면 반드시 갈라진다. 진실값은 이제 레포 한 곳이다.
ROUNDTRIP_PREFIXES = ('_bench_', '_watch_')

# av_collect.py 사본에 따라 있을 수도 없을 수도 있다 — 없으면 SPY만 예약으로 본다.
# (없는 이름을 그냥 참조하면 AttributeError로 죽는다. 사본이 갈라진 자리라서 막아 둔다.)
RESERVED_BENCH = frozenset(getattr(AVC, 'RESERVED_BENCH', {'SPY'}))


def _is_watch_ticker(ticker):
    """가격전용 관심종목(도쿄 4자리·한국 6자리 숫자 코드)인가.

    미국 종목은 알파벳이라 겹치지 않는다. 이 종목들의 이력은 `prices_av/_watch_<코드>.csv`
    로 둔다 — 1층(s1_normalize)·9p층·verify가 '_' 접두를 건너뛰므로 채점 유니버스 달력에
    섞이지 않는다. 실측(2026-09-12): 도쿄 파일 하나가 접두 없이 prices_av에 들어가자
    1층의 「전 종목 49행 이상」·「공동 거래일 44일 이상」 검사가 깨졌다.
    """
    return ticker.isdigit()


def _ticker_files(base):
    """_div_ 접두 파일(배당)은 이 동기화 대상이 아니다 — av_collect.py가 다루는
    시세 파일만 왕복시킨다. 예외는 둘이고, 둘 다 왕복시키지 않으면 트리거 세션마다
    이력이 되감긴다:
      _bench_ (벤치마크 SPY, 2026-09-11) · _watch_ (가격전용 관심종목, 2026-09-12)
    (s1_normalize.py는 여전히 '_' 전체를 건너뛰므로 유니버스에는 안 섞인다.)"""
    out = []
    for p in sorted(glob.glob(os.path.join(_av_dir(base), '*.csv'))):
        name = os.path.basename(p)[:-4]
        if name.startswith('_') and not name.startswith(ROUNDTRIP_PREFIXES):
            continue
        out.append((name, p))
    return out


def _count_backup_rows(path):
    """기존 백업 파일의 데이터 행수. 없거나 못 읽으면 None(비교하지 않는다)."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8-sig', errors='strict') as f:
            rows = list(csv.reader(f))
    except (OSError, UnicodeDecodeError):
        return None
    if not rows or [c.strip() for c in rows[0]] != BACKUP_HEADER:
        return None
    return sum(1 for r in rows[1:] if len(r) == len(BACKUP_HEADER) and r[0].strip())


def cmd_export(outfile, base=AVC.BASE, allow_shrink=False):
    """★ 왜 여기서 막는가 — 2026-09-19에 실측으로 드러난 조용한 실패
      Drive에 `prices_av_backup_2026-09-11.csv`가 **44바이트**로 올라가 있었다.
      44바이트는 `ticker,timestamp,open,high,low,close,volume` 헤더 한 줄 그대로다 —
      데이터 행이 **0개**인 백업이다. 그날 export는 오류를 내지 않았다.
      「■ export — 티커 0개 · 총 0행」을 찍고 종료코드 0으로 끝냈고, 미러링은 그
      빈 파일을 그대로 Drive에 올렸다. 다음 날 같은 이름의 24,287바이트 파일이
      다시 올라와 있었지만, **이름이 같은 파일이 둘**이라 이름으로 복원하면
      빈 쪽을 집을 수 있다.
      백업이 사고에서 값을 하는 유일한 순간은 원본이 이미 없어진 뒤다. 그때 빈
      파일이었다는 걸 알게 되면 늦는다. 그래서 세 가지를 바꾼다:
        ① 데이터 행이 0개면 **쓰지 않고 죽는다**(빈 파일을 남기지 않는다).
        ② 덮어쓸 자리에 이미 있던 백업보다 **행이 줄면 죽는다** — 백업은 줄 수 없다.
           정말 줄어야 할 사정이 있으면 사람이 --allow-shrink로 명시한다.
        ③ 임시 파일에 쓰고 **원자적으로 갈아 끼운다** — 쓰다 죽어도 반쪽 파일이
           남지 않는다.
    """
    rows = []
    tickers_with_data = 0
    for ticker, path in _ticker_files(base):
        data, _note = AVC.read_av_file(path)
        if not data:
            continue
        tickers_with_data += 1
        for d in sorted(data):
            rows.append([ticker] + data[d])

    # ① 빈 백업은 백업이 아니다
    if not rows:
        raise SystemExit(
            "  ✗ 내보낼 시세가 한 행도 없다 — 헤더만 있는 백업은 쓰지 않는다.\n"
            f"      본 곳: {_av_dir(base)}\n"
            "      (2026-09-11에 이렇게 44바이트 백업이 Drive에 올라갔다.)")

    # ② 백업은 줄 수 없다
    old_n = _count_backup_rows(outfile)
    if old_n is not None and len(rows) < old_n and not allow_shrink:
        raise SystemExit(
            f"  ✗ 백업이 줄어든다 — 기존 {old_n}행 → 새 {len(rows)}행. 덮어쓰지 않는다.\n"
            "      수집이 반쪽만 됐을 때 이걸 덮으면 어제치까지 같이 사라진다.\n"
            "      정말 줄여야 하면 --allow-shrink 를 사람이 붙인다.")

    # ③ 임시 파일 → 원자적 교체
    d = os.path.dirname(os.path.abspath(outfile))
    fd, tmp_path = tempfile.mkstemp(dir=d, prefix='.av_backup_', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            w.writerow(BACKUP_HEADER)
            for r in rows:
                w.writerow(r)
        os.replace(tmp_path, outfile)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    shrink_note = ''
    if old_n is not None:
        shrink_note = f" (기존 {old_n}행 → {len(rows)}행)"
    print(f"■ export — 티커 {tickers_with_data}개 · 총 {len(rows)}행{shrink_note} → {outfile}")
    return tickers_with_data, len(rows)


def _csv_line(row):
    """조각 파일에 넣을 한 줄. csv.writer와 같은 따옴표 규칙을 쓰되 줄 길이를 미리 재려고
    문자열로 뽑는다 (줄바꿈은 csv.writer 기본값과 같은 CRLF)."""
    buf = io.StringIO()
    csv.writer(buf, lineterminator='\r\n').writerow(row)
    return buf.getvalue()


def cmd_export_chunked(outprefix, max_bytes, base=AVC.BASE, allow_shrink=False):
    """cmd_export와 같은 데이터를, **조각마다 헤더를 넣어** 여러 파일로 나눠 쓴다.

    ★ 왜 나누는가 (2026-09-15 실측)
      백업이 1,800행·125KB까지 자라자 Drive 업로드가 「한 응답의 출력 한도」를 넘어
      **조용히 실패**했다. Drive에 올리려면 모델이 파일 전체를 한 응답 안에 텍스트로
      만들어야 하는 구조라, 이력이 쌓일수록 이 실패는 반드시 재발한다 — 지시문을
      아무리 잘 써도 피할 수 없다. 그래서 크기로 잘라 올린다.

    ★ 각 조각은 그 자체로 완전한 백업이다
      헤더가 들어 있으므로 조각 하나만으로도 --import가 된다. 받는 쪽은 조각 수만큼
      --import를 반복하면 되고, 합집합 병합이라 **순서와 횟수에 무관하게** 결과가 같다.

    ★ 여기에도 cmd_export와 같은 안전장치를 건다 (2026-09-19)
      조각으로 나눈다고 빈 백업이 괜찮아지지는 않는다. 0행이면 쓰지 않고 죽고,
      직전 조각 묶음보다 행이 줄면 죽는다. 쓰기도 임시 파일 → 원자적 교체다.
      한 행이 헤더+max_bytes보다 큰 경우(사실상 없다)에도 그 행만 담은 조각을 만들어
      무한루프에 빠지지 않는다.
    """
    rows = []
    tickers_with_data = 0
    for ticker, path in _ticker_files(base):
        data, _note = AVC.read_av_file(path)
        if not data:
            continue
        tickers_with_data += 1
        for d in sorted(data):
            rows.append([ticker] + data[d])

    if not rows:
        raise SystemExit(
            "  ✗ 내보낼 시세가 한 행도 없다 — 헤더만 있는 조각을 만들지 않는다.\n"
            f"      본 곳: {_av_dir(base)}")

    # 직전 회차의 조각 묶음과 견준다 — 조각으로 나눠도 백업은 줄 수 없다
    old_n, i = 0, 1
    while os.path.exists(f"{outprefix}_part{i}.csv"):
        n = _count_backup_rows(f"{outprefix}_part{i}.csv")
        old_n += (n or 0)
        i += 1
    if old_n and len(rows) < old_n and not allow_shrink:
        raise SystemExit(
            f"  ✗ 백업이 줄어든다 — 직전 조각 묶음 {old_n}행 → 새 {len(rows)}행. 쓰지 않는다.\n"
            "      정말 줄여야 하면 --allow-shrink 를 사람이 붙인다.")

    header_line = _csv_line(BACKUP_HEADER)
    header_size = len(header_line.encode('utf-8'))
    chunks, cur, cur_size = [], [header_line], header_size
    for r in rows:
        line = _csv_line(r)
        size = len(line.encode('utf-8'))
        if len(cur) > 1 and cur_size + size > max_bytes:
            chunks.append(cur)
            cur, cur_size = [header_line], header_size
        cur.append(line)
        cur_size += size
    if len(cur) > 1 or not chunks:
        chunks.append(cur)

    paths = []
    for i, lines in enumerate(chunks, start=1):
        out = f"{outprefix}_part{i}.csv"
        d = os.path.dirname(os.path.abspath(out))
        fd, tmp_path = tempfile.mkstemp(dir=d, prefix='.av_chunk_', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
                f.write(''.join(lines))
            os.replace(tmp_path, out)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
        paths.append(out)

    print(f"■ export-chunked — 티커 {tickers_with_data}개 · 총 {len(rows)}행 · "
          f"{len(chunks)}개 파일로 분할(각 ≤{max_bytes}바이트, 헤더 포함) → "
          f"{outprefix}_part1.csv ~ _part{len(chunks)}.csv")
    return tickers_with_data, len(rows), paths


def _read_backup(path):
    """백업 CSV → {ticker: {date: row}}. 구조가 기대와 다르면 예외로 죽는다
    (조용히 빈 dict를 돌려주지 않는다 — 이 파일은 신뢰 전제가 sheet_export.csv보다
    낮으므로 검증에 실패하면 바로 알아야 한다)."""
    if not os.path.exists(path):
        raise SystemExit(f"  ✗ 파일이 없다: {path}")
    with open(path, encoding='utf-8-sig', errors='strict') as f:
        rows = list(csv.reader(f))
    if not rows:
        raise SystemExit(f"  ✗ 빈 파일이다: {path}")
    head = [c.strip() for c in rows[0]]
    if head != BACKUP_HEADER:
        raise SystemExit(f"  ✗ 헤더가 예상과 다르다 — Drive 파일이 깨졌을 수 있다.\n"
                          f"      기대: {BACKUP_HEADER}\n      실제: {head}")
    out = {}
    for r in rows[1:]:
        if len(r) != len(BACKUP_HEADER):
            continue
        ticker = r[0].strip()
        rest = [c.strip() for c in r[1:]]
        if not ticker or not rest[0]:
            continue
        out.setdefault(ticker, {})[rest[0]] = rest
    if not out:
        raise SystemExit(
            f"  ✗ 헤더만 있고 데이터 행이 0개다: {path}\n"
            "      「반영 0종목」으로 조용히 성공한 척하지 않는다 — Drive에서 받아온\n"
            "      파일이 빈 백업(2026-09-11의 44바이트 파일 같은 것)일 수 있다.")
    return out


def cmd_import(infile, base=AVC.BASE):
    by_ticker = _read_backup(infile)
    os.makedirs(_av_dir(base), exist_ok=True)
    n_ok, n_new_rows = 0, 0
    thin = []      # 부분 손상 의심 — 막지는 않지만 알린다
    skipped = []   # 접두 없이 들어온 예약 벤치마크
    for ticker, data in sorted(by_ticker.items()):
        # ★ 예약 벤치마크(SPY)가 접두 없이 백업에 들어 있으면 SPY.csv로 쓰지 않는다 —
        #   채점 유니버스 가격 경로에 벤치마크가 섞이는 유일한 입구를 막는다(2026-09-11).
        if ticker in RESERVED_BENCH:
            skipped.append(ticker)
            continue
        # 가격전용 관심종목(숫자 코드)은 _watch_ 접두 파일로 간다. 백업(export)에는 이미
        # 접두가 붙어 있고, 공개 가격 저장소의 이력(GitHub Actions 산출)은 접두 없이
        # 오므로 여기서 붙인다. 안 붙이면 1층 검사가 깨진다(_is_watch_ticker 참고).
        fname = f'_watch_{ticker}' if _is_watch_ticker(ticker) else ticker
        path = os.path.join(_av_dir(base), f'{fname}.csv')
        old, _old_note = AVC.read_av_file(path)
        if old and len(data) < len(old) * 0.5:
            thin.append(f"{ticker}(백업 {len(data)}행 vs 로컬 {len(old)}행)")
        merged = dict(old)
        merged.update(data)          # 합집합 — old에 있던 날짜는 절대 안 없어진다
        n_new_rows += len(merged) - len(old)
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(f"# SOURCE=백업 병합(미국=Alpha Vantage · 도쿄=Yahoo, "
                     f"Drive 백업 또는 GitHub 공개 가격 저장소) "
                     f"MERGED_ON={AVC.today().isoformat()} N={len(merged)} "
                     f"BY=Drive 동기화 가져오기(s0_prices_av_sync.py --import)\n")
            w = csv.writer(f)
            w.writerow(AVC.AV_HEADER)
            for d in sorted(merged, reverse=True):
                w.writerow(merged[d])
        n_ok += 1
    print(f"■ import — {infile}")
    print(f"  반영 {n_ok}종목(신규 {n_new_rows}행)"
          + (f" · ⚠ 부분 손상 의심 {len(thin)}종목({', '.join(thin)}) — 병합은 했으나 확인 권장"
             if thin else "")
          + (f" · ⚠ 예약 벤치마크가 접두 없이 왔다 → 건너뜀: {', '.join(skipped)}"
             f" (_bench_ 접두로만 받는다)" if skipped else ""))
    return n_ok, len(thin), n_new_rows


def self_test():
    tmp = tempfile.mkdtemp(prefix='s0_sync_test_')
    ok, bad = 0, []

    def chk(name, cond, detail=''):
        nonlocal ok
        if cond:
            ok += 1
        else:
            bad.append(f"{name}" + (f" -- {detail}" if detail else ''))

    try:
        av_dir = os.path.join(tmp, 'prices_av')
        os.makedirs(av_dir)

        def write_ticker(name, dates_rows):
            with open(os.path.join(av_dir, f'{name}.csv'), 'w', encoding='utf-8', newline='') as f:
                f.write("# SOURCE=TEST\n")
                w = csv.writer(f)
                w.writerow(AVC.AV_HEADER)
                for d, row in dates_rows.items():
                    w.writerow([d] + row)

        write_ticker('ZZZA', {'2026-09-01': ['1', '2', '0.5', '1.5', '1000'],
                               '2026-09-02': ['1.5', '2.5', '1', '2', '1100']})
        write_ticker('ZZZB', {'2026-09-01': ['5', '6', '4', '5.5', '2000']})
        # _div_ 파일은 왕복 대상이 아니어야 하고, _bench_ 파일(벤치마크)은 왕복 대상이어야 한다
        write_ticker('_div_ZZZA', {'2026-09-01': ['0', '0', '0', '0.2', '0']})
        write_ticker('_bench_ZZZ', {'2026-09-01': ['9', '9', '9', '9', '9']})

        backup = os.path.join(tmp, 'backup.csv')
        n_tickers, n_rows = cmd_export(backup, base=tmp)
        chk('export가 _div_ 파일을 제외하고 _bench_ 파일은 포함한다', n_tickers == 3, f'{n_tickers}개 나옴 (기대 3)')
        chk('export 총 행수가 맞다', n_rows == 4, f'{n_rows}행 (기대 4)')

        with open(backup, encoding='utf-8-sig') as f:
            head = next(csv.reader(f))
        chk('export 헤더가 BACKUP_HEADER와 같다', head == BACKUP_HEADER, str(head))

        # 롤트립: 같은 파일을 다시 import해도 데이터가 안 줄어야 한다(멱등)
        n_ok, n_thin, n_new = cmd_import(backup, base=tmp)
        chk('같은 백업을 재import해도 전부 반영된다', n_ok == 3, f'ok={n_ok} (기대 3 -- _bench_ 포함)')
        chk('이미 있는 데이터라 신규행은 0이다(멱등성)', n_new == 0, f'new={n_new}')

        # 더 새 데이터가 섞인 백업을 import하면 병합되어야 한다(행이 늘어야 함)
        with open(backup, 'a', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            w.writerow(['ZZZA', '2026-09-03', '2', '3', '1.5', '2.5', '1200'])
        n_ok2, n_thin2, n_new2 = cmd_import(backup, base=tmp)
        chk('신규 날짜가 섞인 백업을 import하면 신규행이 잡힌다', n_new2 == 1, f'new={n_new2}')

        # 한 티커만 담긴 부분(=다른 티커 기준 행수가 뚜렷이 적은) 백업 — 병합은 되지만
        # 로컬 데이터가 사라지면 안 되고(합집합 안전장치), 부분 손상 의심으로 표시돼야 한다
        partial = os.path.join(tmp, 'partial.csv')
        with open(partial, 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            w.writerow(BACKUP_HEADER)
            w.writerow(['ZZZA', '2026-09-01', '1', '2', '0.5', '1.5', '1000'])  # 로컬엔 3행 있음
        n_ok3, n_thin3, n_new3 = cmd_import(partial, base=tmp)
        chk('행수가 뚜렷이 적은 백업은 부분 손상 의심으로 표시된다', n_thin3 == 1, f'thin={n_thin3}')
        chk('그래도 신규행 0이다(이미 있던 날짜라 병합에 문제 없음)', n_new3 == 0, f'new={n_new3}')
        after, _ = AVC.read_av_file(os.path.join(av_dir, 'ZZZA.csv'))
        chk('합집합 병합이라 로컬 데이터는 줄지 않는다(3행 유지 — 부분백업의 09-01은 이미 있던 날짜)',
            len(after) == 3, f'{len(after)}행')

        # 구조가 깨진 백업(헤더 다름)은 예외로 죽어야 한다 — 조용히 넘어가면 안 된다
        broken = os.path.join(tmp, 'broken.csv')
        with open(broken, 'w', encoding='utf-8', newline='') as f:
            f.write("완전히,다른,헤더\n1,2,3\n")
        try:
            cmd_import(broken, base=tmp)
            chk('헤더가 깨진 백업은 SystemExit로 죽는다', False, '예외가 안 났다')
        except SystemExit:
            chk('헤더가 깨진 백업은 SystemExit로 죽는다', True)

        # ── 2026-09-19 추가: Drive의 44바이트 백업을 잡는 검사들 ──────────────
        # ① 시세가 한 행도 없으면 export가 빈 파일을 남기지 않고 죽어야 한다
        empty_base = os.path.join(tmp, 'empty_ws')
        os.makedirs(os.path.join(empty_base, 'prices_av'))
        empty_out = os.path.join(tmp, 'would_be_44bytes.csv')
        try:
            cmd_export(empty_out, base=empty_base)
            chk('시세가 0행이면 export가 죽는다(44바이트 파일 방지)', False, '그냥 써 버렸다')
        except SystemExit:
            chk('시세가 0행이면 export가 죽는다(44바이트 파일 방지)', True)
        chk('죽었을 때 빈 파일을 남기지 않는다', not os.path.exists(empty_out),
            '파일이 생겼다')

        # ② 백업은 줄 수 없다 — 기존 백업보다 행이 적으면 덮지 않는다
        shrink_target = os.path.join(tmp, 'shrink.csv')
        cmd_export(shrink_target, base=tmp)            # 4행짜리 정상 백업
        before = open(shrink_target, encoding='utf-8').read()
        thin_base = os.path.join(tmp, 'thin_ws')
        os.makedirs(os.path.join(thin_base, 'prices_av'))
        with open(os.path.join(thin_base, 'prices_av', 'ZZZA.csv'), 'w',
                  encoding='utf-8', newline='') as f:
            f.write("# SOURCE=TEST\n")
            w = csv.writer(f)
            w.writerow(AVC.AV_HEADER)
            w.writerow(['2026-09-01', '1', '2', '0.5', '1.5', '1000'])
        try:
            cmd_export(shrink_target, base=thin_base)  # 1행 — 줄어든다
            chk('행이 줄어드는 export는 죽는다', False, '덮어써 버렸다')
        except SystemExit:
            chk('행이 줄어드는 export는 죽는다', True)
        chk('막혔을 때 기존 백업이 그대로다',
            open(shrink_target, encoding='utf-8').read() == before, '내용이 바뀌었다')
        n_t, n_r = cmd_export(shrink_target, base=thin_base, allow_shrink=True)
        chk('사람이 --allow-shrink를 붙이면 줄여서 쓸 수 있다', n_r == 1, f'{n_r}행')

        # ③ 헤더만 있는 백업을 import하면 「반영 0종목」이 아니라 죽어야 한다
        header_only = os.path.join(tmp, 'header_only.csv')
        with open(header_only, 'w', encoding='utf-8', newline='') as f:
            csv.writer(f).writerow(BACKUP_HEADER)
        # Drive의 그 파일은 44바이트였다 = 헤더 43자 + 줄바꿈 1자(LF).
        # csv.writer는 CRLF를 쓰므로 여기선 45바이트가 된다 — 둘 다 「헤더만」이다.
        chk('헤더만 있는 파일은 44~45바이트다(Drive의 그 파일과 같은 모양)',
            os.path.getsize(header_only) in (44, 45),
            f'{os.path.getsize(header_only)}바이트')
        try:
            cmd_import(header_only, base=tmp)
            chk('헤더만 있는 백업을 import하면 죽는다', False, '조용히 0종목으로 끝났다')
        except SystemExit:
            chk('헤더만 있는 백업을 import하면 죽는다', True)

        # ── 2026-09-19 합본: Drive 사본에만 있던 기능들 ─────────────────────
        # ① _watch_ 접두 — 도쿄·한국 숫자 코드가 채점 유니버스에 섞이지 않아야 한다
        chk('숫자 코드는 가격전용 관심종목으로 보고 알파벳은 아니다',
            _is_watch_ticker('6503') and _is_watch_ticker('010140')
            and not _is_watch_ticker('AAPL'))
        write_ticker('_watch_6503', {'2026-09-01': ['1', '1', '1', '1', '1']})
        chk('_watch_ 접두 파일은 왕복 대상이다 (안 그러면 매 실행 이력이 되감긴다)',
            '_watch_6503' in [n for n, _ in _ticker_files(tmp)])
        chk('_div_ 접두는 여전히 왕복 대상이 아니다',
            not any(n.startswith('_div_') for n, _ in _ticker_files(tmp)))

        jp = os.path.join(tmp, 'jp.csv')
        with open(jp, 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            w.writerow(BACKUP_HEADER)
            w.writerow(['6503', '2026-09-02', '2', '2', '2', '2', '2'])
            w.writerow(['SPY', '2026-09-02', '9', '9', '9', '9', '9'])
        cmd_import(jp, base=tmp)
        chk('접두 없이 온 숫자 코드는 _watch_ 파일로 들어간다',
            os.path.exists(os.path.join(av_dir, '_watch_6503.csv')))
        chk('접두 없이 온 숫자 코드가 유니버스 경로(6503.csv)로는 안 들어간다',
            not os.path.exists(os.path.join(av_dir, '6503.csv')))
        chk('접두 없는 예약 벤치마크(SPY)는 건너뛴다 — 유니버스에 섞이는 입구를 막는다',
            not os.path.exists(os.path.join(av_dir, 'SPY.csv')))

        # ② 조각 내보내기 — 조각 하나하나가 그 자체로 완전한 백업이어야 한다
        pre = os.path.join(tmp, 'chunked')
        n_t, n_r, paths = cmd_export_chunked(pre, max_bytes=120, base=tmp)
        chk('조각이 둘 이상으로 나뉜다', len(paths) >= 2, f'{len(paths)}개')
        heads = [next(csv.reader(open(q, encoding='utf-8-sig'))) for q in paths]
        chk('조각마다 헤더가 들어 있다 (하나만으로도 --import 된다)',
            all(h == BACKUP_HEADER for h in heads))
        chk('조각 행수의 합이 전체 행수와 같다',
            sum(_count_backup_rows(q) for q in paths) == n_r,
            f'{sum(_count_backup_rows(q) for q in paths)} vs {n_r}')

        # 순방향·역방향 어느 순서로 넣어도 결과가 같다 (합집합 병합)
        fresh = os.path.join(tmp, 'ws_fwd')
        os.makedirs(os.path.join(fresh, 'prices_av'))
        for q in paths:
            cmd_import(q, base=fresh)
        fwd = os.path.join(tmp, 'fwd.csv')
        _, fwd_rows = cmd_export(fwd, base=fresh)
        back = os.path.join(tmp, 'ws_rev')
        os.makedirs(os.path.join(back, 'prices_av'))
        for q in reversed(paths):
            cmd_import(q, base=back)
        rev = os.path.join(tmp, 'rev.csv')
        _, rev_rows = cmd_export(rev, base=back)
        chk('조각을 어떤 순서로 넣어도 행수가 같다', fwd_rows == rev_rows,
            f'{fwd_rows} vs {rev_rows}')
        chk('조각을 다 넣으면 원본 행수를 회복한다', fwd_rows == n_r,
            f'{fwd_rows} vs {n_r}')

        # ③ 조각 쪽에도 빈 백업 금지가 걸려 있다
        empty2 = os.path.join(tmp, 'empty_ws2')
        os.makedirs(os.path.join(empty2, 'prices_av'))
        try:
            cmd_export_chunked(os.path.join(tmp, 'never'), 1000, base=empty2)
            chk('시세가 0행이면 조각 내보내기도 죽는다', False, '그냥 썼다')
        except SystemExit:
            chk('시세가 0행이면 조각 내보내기도 죽는다', True)
        chk('죽었을 때 조각 파일을 안 남긴다',
            not os.path.exists(os.path.join(tmp, 'never_part1.csv')))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n■ self_test — {ok} OK · {len(bad)} NG")
    for b in bad:
        print(f"  ✗ {b}")
    return len(bad) == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--export', metavar='OUTFILE')
    ap.add_argument('--export-chunked', metavar='OUTPREFIX',
                    help='조각마다 헤더를 넣어 여러 파일로 나눠 쓴다 (Drive 업로드 한도 때문)')
    ap.add_argument('--max-bytes', type=int, default=60000,
                    help='--export-chunked 조각 하나의 최대 바이트 (기본 60000)')
    ap.add_argument('--import', dest='import_file', metavar='INFILE')
    ap.add_argument('--self-test', action='store_true')
    ap.add_argument('--allow-shrink', action='store_true',
                    help='기존 백업보다 행이 줄어도 덮어쓴다 — 사람이 사정을 알 때만')
    a = ap.parse_args()
    if a.self_test:
        sys.exit(0 if self_test() else 1)
    elif a.export:
        cmd_export(a.export, allow_shrink=a.allow_shrink)
    elif a.export_chunked:
        cmd_export_chunked(a.export_chunked, a.max_bytes, allow_shrink=a.allow_shrink)
    elif a.import_file:
        cmd_import(a.import_file)
    else:
        ap.print_help()


if __name__ == '__main__':
    main()


