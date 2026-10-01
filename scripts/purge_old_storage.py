"""
published_at이 지정한 기간(START_DATE ~ END_DATE) 안에 속하는 영상들의 mp3를
Storage에서 완전히 삭제한다.

Storage 대시보드에서 폴더째 삭제하면 가끔 파일이 orphan으로 남아 용량이 안 줄어드는
문제가 있어서, 폴더 안 파일 목록을 직접 조회해서 하나씩 remove() 하는 방식으로 처리.

필요한 환경변수:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
  PURGE_START_DATE (예: "2025-01-01")
  PURGE_END_DATE   (예: "2025-04-01", 이 날짜는 미포함 - 즉 2025년 1~3월만 지우려면
                     START=2025-01-01, END=2025-04-01)
"""
import os
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
STORAGE_BUCKET = "coldsheep-piano"
PURGE_START_DATE = os.environ["PURGE_START_DATE"]
PURGE_END_DATE = os.environ["PURGE_END_DATE"]

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def main():
    result = (
        supabase.table("coldsheep_videos")
        .select("video_id, title, published_at, output_path")
        .gte("published_at", PURGE_START_DATE)
        .lt("published_at", PURGE_END_DATE)
        .eq("status", "done")
        .execute()
    )
    rows = result.data or []
    print(f"삭제 대상: {len(rows)}개 ({PURGE_START_DATE} ~ {PURGE_END_DATE} 미만)")
    deleted = 0
    errors = []
    for row in rows:
        video_id = row["video_id"]
        try:
            files = supabase.storage.from_(STORAGE_BUCKET).list(video_id)
            if not files:
                print(f"{video_id}: 이미 비어있음 (스킵)")
            else:
                paths = [f"{video_id}/{f['name']}" for f in files]
                supabase.storage.from_(STORAGE_BUCKET).remove(paths)
                print(f"{video_id}: {len(paths)}개 파일 삭제 -> {paths}")
            # status='purged'로 바꿔서 process_video.py(status='pending'만 처리)가
            # 다시 집어가지 않게 홀드. list_videos.py도 이미 존재하는 video_id는
            # 건드리지 않으므로(ignore_duplicates), 재수집/재다운로드 되지 않는다.
            supabase.table("coldsheep_videos").update(
                {"status": "purged", "output_path": None}
            ).eq("video_id", video_id).execute()
            deleted += 1
        except Exception as e:
            print(f"{video_id}: 실패 - {e}")
            errors.append(video_id)
    print(f"\n완료: {deleted}개 처리, 실패 {len(errors)}개")
    if errors:
        print("실패 목록:", ", ".join(errors))


if __name__ == "__main__":
    main()
