# code/ — 예약 세션이 쓰는 스크립트 두 개

이 폴더는 **사본**입니다. 진실값은 비공개 저장소 `multica-quant-ops` 이고,
여기 있는 파일은 거기서 복사해 온 것입니다. 여기서 고치지 마십시오.

## 왜 여기에 있나 (2026-09-21)

「AI투자시스템 주간 통합 점검」 예약 작업은 매번 빈 컨테이너에서 뜹니다.
그 컨테이너는 비공개 저장소를 clone할 수 없고(자격증명이 없습니다), 공개 저장소는
clone할 수 있습니다. 그래서 0단계가 쓸 스크립트를 여기에 둡니다.

그 전까지는 Google Drive에서 받아 썼는데, **2026-09-20 실행에서 실제로 깨졌습니다.**
Drive에서 내려받은 `av_collect.py`의 base64 변환이 두 차례 손상되자 그 세션이
필요한 함수만 **스스로 다시 짜서** 썼습니다. 아무도 쓰거나 검토하지 않은 코드로
그 주 가격 반영이 돌아간 것입니다. git으로 받으면 그 일이 구조적으로 불가능합니다.

## 무엇이 들어 있나

| 파일 | 하는 일 |
|---|---|
| `s0_prices_av_sync.py` | `prices_av_backup.csv` ↔ `prices_av/*.csv` 왕복 병합(합집합). `--self-test`로 자체검사 30개를 돕니다 |
| `av_collect.py` | 위 스크립트가 import합니다(`BASE`·`read_av_file`·`AV_HEADER`·`today`) |

`valuation_models/price_keys.json` 은 **여기 없습니다.** 예약 세션은 Alpha Vantage를
부르지 않으므로 그 파일을 읽는 함수를 쓰지 않습니다(모듈을 import하는 것만으로는
읽지 않는다는 것을 실측으로 확인했습니다).

## 쓰는 법 (예약 작업 0단계)

```
rm -rf /tmp/prices && git clone --depth 1 https://github.com/shinbuheon-prog/multica-quant-prices.git /tmp/prices
mkdir -p /home/claude && cp /tmp/prices/code/*.py /home/claude/
cd /home/claude && python3 s0_prices_av_sync.py --self-test     # 0 NG 확인
python3 s0_prices_av_sync.py --import /tmp/prices/prices_av_backup.csv
```

## 사본이 낡았는지 확인하는 법

복사해 온 시점: `multica-quant-ops` @ `d7751ae` (2026-09-21)

```
sha256sum code/*.py | cut -c1-16
  s0_prices_av_sync.py  94e30d2fca807f0b
  av_collect.py         4bc88436178612e7
```

비공개 저장소에서 같은 명령을 돌려 값이 다르면 이 사본이 낡은 것입니다.
그때는 비공개 저장소의 파일을 다시 복사해 push하십시오 — 반대 방향은 없습니다.
