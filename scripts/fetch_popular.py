"""
책콩 앱용 '또래 인기 도서' 데이터 만들기 (매일 새벽 GitHub Actions 에서 실행)

도서관 정보나루 인기대출도서(loanItemSrch)를 나이(2~19살) × 기간(1·3·12개월)마다 한 번씩 받아
popular/age{N}.json 으로 저장한다. 분야(이야기·과학 등)는 앱이 class_no 로 나눠 보여 준다.
→ 하루 호출 54건 남짓 (책이 적은 나이는 앞뒤 한 살로 넓혀 한 번 더).

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
OUT = "popular"


def months_ago(d: date, months: int) -> date:
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    day = min(d.day, 28)
    return date(y, m, day)


def get(url: str, timeout: int = 60, tries: int = 2) -> dict:
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


def fetch(start: date, end: date, from_age: int, to_age: int) -> list:
    q = urllib.parse.urlencode({
        "authKey": KEY, "startDt": start.isoformat(), "endDt": end.isoformat(),
        "from_age": from_age, "to_age": to_age, "pageSize": PAGE_SIZE, "format": "json",
    })
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
        })
        if len(books) >= KEEP:
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
    results = {}
    for age in AGES:
        periods = {}
        for m in PERIODS:
            start = months_ago(end, m)
            books = fetch(start, end, age, age); calls += 1
            widened = False
            if len(books) < MIN_BOOKS:
                books = fetch(start, end, max(age - 1, 0), age + 1); calls += 1
                widened = True
            with_class += sum(1 for b in books if b["k"])
            periods[str(m)] = {"widened": widened, "books": books}
            print(f"{age}살 {m}개월: {len(books)}권{' (앞뒤 한 살 포함)' if widened else ''}", flush=True)
            time.sleep(1)
        results[age] = periods

    total = sum(len(p["books"]) for ps in results.values() for p in ps.values())
    print(f"호출 {calls}건, 책 {total}권, 분류번호 있는 책 {with_class}권")
    # 이상한 결과면 덮어쓰지 않는다
    if total < len(AGES) * len(PERIODS) * 5:
        print("받은 책이 너무 적어 저장하지 않습니다 (어제 파일 유지).")
        return 1

    for age, periods in results.items():
        doc = {"updated": today.isoformat(), "age": age, "source": "도서관 정보나루", "periods": periods}
        with open(f"{OUT}/age{age}.json", "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        written += 1
    with open(f"{OUT}/index.json", "w", encoding="utf-8") as f:
        json.dump({"updated": today.isoformat(), "ages": list(AGES), "periods": list(PERIODS)}, f)
    print(f"파일 {written}개 저장")
    return 0


if __name__ == "__main__":
    sys.exit(main())
