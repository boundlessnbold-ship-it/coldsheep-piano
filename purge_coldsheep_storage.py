#!/usr/bin/env python3
"""
coldsheep-piano 버킷에서 오래된 영상의 mp3 를 정리합니다.

경로 규칙이 <video_id>/piano_only.mp3 이고 영상당 파일 1개이므로,
coldsheep_videos.published_at 으로 대상을 고른 뒤 Storage API 로 지웁니다.

중요 1) SQL 로 storage.objects 를 delete 하면 안 됩니다. 그건 메타데이터일
        뿐이고 실제 파일은 S3 백엔드에 고아로 남아 용량이 그대로입니다.
        반드시 Storage API 를 거쳐야 합니다.

중요 2) DB 행은 지우지 않고 status 를 'purged' 로 바꿉니다. 행을 지우면
        일일 파이프라인이 그 영상을 미처리로 보고 다시 다운로드·업로드해서
        용량이 되돌아옵니다. 파이프라인이 status 로 처리 대상을 거르는지
        먼저 확인하세요.

사용법
    export SUPABASE_URL="https://xxxx.supabase.co"
    export SUPABASE_SERVICE_KEY="eyJ..."

    python purge_coldsheep_storage.py                    # 미리보기 (기본)
    python purge_coldsheep_storage.py --before 2022-01-01
    python purge_coldsheep_storage.py --execute          # 실제 삭제
    python purge_coldsheep_storage.py --execute --keep-rows   # status 변경 안 함

중단되어도 다시 실행하면 남은 것부터 이어서 진행합니다.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BUCKET = "coldsheep-piano"
TABLE = "coldsheep_videos"
FILENAME = "piano_only.mp3"
BATCH = 90                      # Storage 삭제 1회 요청당 개수


def env(name):
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"환경변수 {name} 가 없습니다.")
    return v.rstrip("/") if name.endswith("URL") else v


def req(method, url, key, body=None, ok=(200, 201, 204)):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers={
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            raw = resp.read().decode(errors="replace")
            if resp.status not in ok:
                sys.exit(f"HTTP {resp.status}: {raw[:400]}")
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} 실패 HTTP {e.code}: {e.read().decode(errors='replace')[:400]}")


def fetch_targets(url, key, before):
    """삭제 대상 영상 목록. PostgREST 페이지네이션으로 전부 가져옵니다."""
    out, offset = [], 0
    while True:
        q = (f"{url}/rest/v1/{TABLE}"
             f"?select=video_id,title,published_at,status"
             f"&published_at=lt.{before}"
             f"&order=published_at.asc"
             f"&offset={offset}&limit=1000")
        page = req("GET", q, key) or []
        out += page
        if len(page) < 1000:
            return out
        offset += 1000


def fetch_sizes(url, key, video_ids):
    """버킷에 실제로 존재하는 파일과 크기. 없는 건 이미 지워진 것입니다."""
    sizes = {}
    for vid in video_ids:
        body = {"prefix": vid, "limit": 100}
        res = req("POST", f"{url}/storage/v1/object/list/{BUCKET}", key, body) or []
        for obj in res:
            if obj.get("name") == FILENAME:
                meta = obj.get("metadata") or {}
                sizes[vid] = int(meta.get("size") or 0)
    return sizes


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", default="2022-01-01",
                    help="이 날짜보다 이전에 게시된 영상 (기본 2022-01-01)")
    ap.add_argument("--execute", action="store_true",
                    help="실제로 삭제합니다. 없으면 미리보기만 합니다.")
    ap.add_argument("--keep-rows", action="store_true",
                    help="DB status 를 바꾸지 않습니다 (재처리 위험).")
    a = ap.parse_args()

    url, key = env("SUPABASE_URL"), env("SUPABASE_SERVICE_KEY")

    rows = fetch_targets(url, key, a.before)
    if not rows:
        print(f"{a.before} 이전 영상이 없습니다.")
        return
    print(f"대상 영상 {len(rows)}건 "
          f"({rows[0]['published_at'][:10]} ~ {rows[-1]['published_at'][:10]})")

    print("버킷 확인 중...")
    sizes = fetch_sizes(url, key, [r["video_id"] for r in rows])
    total = sum(sizes.values())
    print(f"  파일 있음 {len(sizes)}건 · 회수 예상 {human(total)}")
    print(f"  파일 없음 {len(rows) - len(sizes)}건 (이미 삭제됨 또는 미처리)")

    if not sizes:
        print("지울 파일이 없습니다.")
        return

    if not a.execute:
        for vid in list(sizes)[:5]:
            t = next(r["title"] for r in rows if r["video_id"] == vid)
            print(f"    {vid}/{FILENAME}  {human(sizes[vid]):>8}  {t[:40]}")
        if len(sizes) > 5:
            print(f"    ... 외 {len(sizes) - 5}건")
        print(f"\n[미리보기] 삭제하지 않았습니다. 실행하려면 --execute 를 붙이세요.")
        return

    # --- 1) Storage 파일 삭제 ---
    paths = [f"{v}/{FILENAME}" for v in sizes]
    done = 0
    for i in range(0, len(paths), BATCH):
        chunk = paths[i:i + BATCH]
        req("DELETE", f"{url}/storage/v1/object/{BUCKET}", key, {"prefixes": chunk})
        done += len(chunk)
        print(f"  파일 삭제 {done}/{len(paths)}")

    # --- 2) DB status 변경 ---
    if a.keep_rows:
        print("\n--keep-rows: DB 를 건드리지 않았습니다. "
              "파이프라인이 재처리하지 않는지 확인하세요.")
    else:
        ids = ",".join(f'"{v}"' for v in sizes)
        patch = f"{url}/rest/v1/{TABLE}?video_id=in.({ids})"
        r = urllib.request.Request(
            patch,
            data=json.dumps({"status": "purged", "output_path": None}).encode(),
            method="PATCH",
            headers={"apikey": key, "Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "Prefer": "return=minimal"})
        try:
            with urllib.request.urlopen(r, timeout=120) as resp:
                print(f"  DB status='purged' 갱신 (HTTP {resp.status})")
        except urllib.error.HTTPError as e:
            print(f"  경고: DB 갱신 실패 HTTP {e.code}: "
                  f"{e.read().decode(errors='replace')[:300]}\n"
                  f"  파일은 지워졌습니다. 수동으로 status 를 바꿔주세요.",
                  file=sys.stderr)

    print(f"\n완료. {len(sizes)}건 · {human(total)} 회수")


if __name__ == "__main__":
    main()
