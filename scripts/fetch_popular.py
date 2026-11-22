"""
책콩 앱용 '또래 인기 도서' 데이터 만들기 (매일 새벽 GitHub Actions 에서 실행)

도서관 정보나루 인기대출도서(loanItemSrch)를 나이(2~19살) × 기간(1·3·12개월)마다 한 번씩 받아
popular/age{N}.json 으로 저장한다. 분야(이야기·과학 등)는 앱이 class_no 로 나눠 보여 준다.
부모님 추천용으로 30~49세 인기 도서(전체)와 육아책(KDC 598·379 등)을 popular/parent.json 에 따로 저장한다 (+3건).
→ 하루 호출 54건 + 분야별 90건 + 부모님 3건 남짓 (책이 적은 나이는 앞뒤 한 살로 넓혀 한 번 더).

받기에 실패하거나 결과가 이상하면 파일을 덮어쓰지 않고 실패로 끝난다 (어제 파일 유지).
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

KEY = os.environ.get("LIBRARY_KEY", "").strip()
AGES = range(2, 20)
PERIODS = (1, 3, 12)          # 개월
PAGE_SIZE = 200               # 분야별로 나눠도 충분히 남게
KEEP = 150                    # 시리즈 중복을 뺀 뒤 기간당 남길 책 수
MIN_BOOKS = 10                # 이보다 적으면 앞뒤 한 살까지 넓힌다
TOPIC_PERIOD = 3              # 이 기간은 분야별로도 따로 받는다 (문학 외 분야가 전체 목록에선 너무 적어서)
TOPICS = ("4", "3", "6", "9", "5")   # KDC: 자연과학·사회과학·예술·역사·기술과학(건강·생활)
TOPIC_KEEP = 40
OUT = "popular"


def months_ago(d: date, months: int) -> date:
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    day = min(d.day, 28)
    return date(y, m, day)


def get(url: str, timeout: int = 60, tries: int = 3) -> dict:
    last = None
    for attempt in range(tries):
        t0 = time.time()
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                body = json.loads(r.read().decode("utf-8"))
            print(f"  응답 {time.time() - t0:.1f}초", flush=True)
            return body
        except Exception as e:  # 느린 서버: 잠깐 쉬고 다시
            last = e
            print(f"  실패 {time.time() - t0:.1f}초: {type(e).__name__} {e}", flush=True)
            time.sleep(5)
    raise RuntimeError(f"요청 실패 ({last})")


def clean_author(raw: str) -> str:
    parts = []
    for p in re.split(r"[;,]", raw or ""):
        p = re.sub(r"^\s*(원작|지은이|글·그림|글그림|글|그림|옮긴이|엮은이|지음|저자|기획)\s*[:：]?\s*", "", p).strip()
        p = re.sub(r"\s*(글·그림|글그림|지음|글|그림|저|옮김|역)$", "", p).strip()
        if p and p not in parts:
            parts.append(p)
    return ", ".join(parts[:2])


def series_key(title: str) -> str:
    t = title.lower()
    t = re.sub(r"[\s.·:\-]*(\d+|[0-9]+권|vol\.?\s*\d+)$", "", t)
    return re.sub(r"\s+", "", t)


def fetch(start: date, end: date, from_age: int, to_age: int, kdc: str = "", keep: int = KEEP) -> list:
    params = {
        "authKey": KEY, "startDt": start.isoformat(), "endDt": end.isoformat(),
        "from_age": from_age, "to_age": to_age, "pageSize": PAGE_SIZE if not kdc else 80, "format": "json",
    }
    if kdc:
        params["kdc"] = kdc
    q = urllib.parse.urlencode(params)
    body = get(f"https://data4library.kr/api/loanItemSrch?{q}")
    res = body.get("response") or {}
    if "error" in res:
        raise RuntimeError(f"정보나루 오류: {res.get('errCode')} {res.get('error')}")
    books, seen = [], set()
    for d in res.get("docs") or []:
        b = d.get("doc") or {}
        title = (b.get("bookname") or "").strip()
        if not title or series_key(title) in seen:
            continue
        seen.add(series_key(title))
        books.append({
            "t": title,
            "a": clean_author(b.get("authors") or ""),
            "i": (b.get("isbn13") or "").strip(),
            "c": (b.get("bookImageURL") or "").strip().replace("http://", "https://"),
            "n": int(str(b.get("loan_count") or "0").replace(",", "") or 0),
            "k": (b.get("class_no") or "").strip()[:1],   # KDC 대분류 숫자 (분야 나누기용)
            "cn": (b.get("class_no") or "").strip(),       # 전체 분류번호 (부모 육아책 고르기용, 저장 전에 지움)
        })
        if len(books) >= keep:
            break
    return books


def main() -> int:
    if not KEY:
        print("LIBRARY_KEY 비밀값이 없습니다. 저장소 Settings › Secrets › Actions 에 넣어 주세요.")
        return 1
    today = date.today()
    end = today - timedelta(days=1)
    # 먼저 작은 요청 하나로 연결을 확인한다 (막혀 있으면 오래 끌지 않고 바로 실패)
    print("연결 확인…", flush=True)
    probe = urllib.parse.urlencode({"authKey": KEY, "startDt": months_ago(end, 1).isoformat(),
                                    "endDt": end.isoformat(), "from_age": 6, "to_age": 6,
                                    "pageSize": 3, "format": "json"})
    try:
        res = get(f"https://data4library.kr/api/loanItemSrch?{probe}", timeout=60, tries=1).get("response") or {}
    except Exception as e:
        print(f"정보나루에 연결하지 못했습니다: {e}")
        return 1
    if "error" in res:
        print(f"정보나루 오류: {res.get('errCode')} {res.get('error')}")
        return 1
    sample = [(d.get('doc') or {}) for d in res.get('docs') or []]
    print("샘플:", [(b.get('bookname'), b.get('class_no')) for b in sample], flush=True)
    os.makedirs(OUT, exist_ok=True)
    calls, written, with_class = 0, 0, 0
    # 부모님 추천은 호출이 적으니 먼저 (아이 파일 받다가 시간이 다 가도 이건 남게)
    parent_ok = save_parent(today, end)
    results, skipped = {}, []
    for age in AGES:
        periods = {}
        try:
          for m in PERIODS:
              start = months_ago(end, m)
              books = fetch(start, end, age, age); calls += 1
              widened = False
              if len(books) < MIN_BOOKS:
                  books = fetch(start, end, max(age - 1, 0), age + 1); calls += 1
                  widened = True
              with_class += sum(1 for b in books if b["k"])
              periods[str(m)] = {"widened": widened, "books": books}
              if m == TOPIC_PERIOD:
                  by_topic = {}
                  for k in TOPICS:
                      lo, hi = (max(age - 1, 0), age + 1) if widened else (age, age)
                      try:
                          by_topic[k] = fetch(start, end, lo, hi, kdc=k, keep=TOPIC_KEEP); calls += 1
                      except Exception as e:  # 분야 하나 실패해도 전체는 계속
                          print(f"  {age}살 분야 {k} 실패: {e}", flush=True)
                      time.sleep(1)
                  periods[str(m)]["byTopic"] = by_topic
                  print(f"  분야별: " + ", ".join(f"{k}={len(v)}" for k, v in by_topic.items()), flush=True)
              print(f"{age}살 {m}개월: {len(books)}권{' (앞뒤 한 살 포함)' if widened else ''}", flush=True)
              time.sleep(1)
        except Exception as e:  # 한 나이가 끝내 실패하면 그 나이는 어제 파일을 그대로 둔다
            print(f"{age}살 실패, 건너뜀 (어제 파일 유지): {e}", flush=True)
            skipped.append(age)
            continue
        results[age] = periods

    total = sum(len(p["books"]) for ps in results.values() for p in ps.values())
    print(f"호출 {calls}건, 책 {total}권, 분류번호 있는 책 {with_class}권")
    # 이상한 결과면 덮어쓰지 않는다
    if not results or total < len(results) * len(PERIODS) * 5:
        print("받은 책이 너무 적어 저장하지 않습니다 (어제 파일 유지).")
        return 0 if parent_ok else 1   # 부모님 파일이라도 새로 받았으면 올리게

    for periods in results.values():
        for p in periods.values():
            for b in p["books"] + [x for v in p.get("byTopic", {}).values() for x in v]:
                b.pop("cn", None)
    for age, periods in results.items():
        doc = {"updated": today.isoformat(), "age": age, "source": "도서관 정보나루", "periods": periods}
        with open(f"{OUT}/age{age}.json", "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        written += 1
    with open(f"{OUT}/index.json", "w", encoding="utf-8") as f:
        json.dump({"updated": today.isoformat(), "ages": list(AGES), "periods": list(PERIODS)}, f)
    print(f"파일 {written}개 저장" + (f", 실패해서 건너뛴 나이 {skipped}" if skipped else ""))
    return 0


PARENT_AGES = (30, 49)
PARENTING = ("598", "379", "599")   # 육아 / 가정교육·유아교육 / 아동


def save_parent(today: date, end: date) -> bool:
    """부모님 추천: 30~49세 인기 도서 + 육아책. 실패해도 아이 파일은 이미 저장됐으니 넘어간다."""
    start = months_ago(end, 3)
    try:
        lo, hi = PARENT_AGES
        adult = [b for b in fetch(start, end, lo, hi, keep=80) if not b["cn"].startswith(PARENTING)]
        parenting = []
        for kdc in ("5", "3"):              # 기술과학(598 육아), 사회과학(379 가정교육)
            for b in fetch(start, end, lo, hi, kdc=kdc, keep=200):
                if b["cn"].startswith(PARENTING) and b["t"] not in {x["t"] for x in parenting}:
                    parenting.append(b)
            time.sleep(1)
        parenting.sort(key=lambda b: -b["n"])
        for b in adult + parenting:
            b.pop("cn", None)
        print(f"부모님: 인기 {len(adult)}권, 육아 {len(parenting)}권", flush=True)
        if len(adult) < 5:
            print("부모님 인기 도서가 너무 적어 저장하지 않습니다.")
            return False
        doc = {"updated": today.isoformat(), "ages": list(PARENT_AGES), "source": "도서관 정보나루",
               "adult": adult[:40], "parenting": parenting[:40]}
        with open(f"{OUT}/parent.json", "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        print("parent.json 저장")
        return True
    except Exception as e:
        print(f"부모님 추천 받기 실패 (아이 파일은 그대로): {e}", flush=True)
        return False


if __name__ == "__main__":
    sys.exit(main())
