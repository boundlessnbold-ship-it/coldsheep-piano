"""
published_at이 2022-12-31 이전인 영상들의 mp3를 Storage에서 완전히 삭제한다.
Supabase Storage 대시보드에서 폴더째로 지울 때 하위 파일이 다 안 지워지고
남는 경우가 있어서, 폴더 안 파일 목록을 직접 조회해서 확실하게 지운다.

삭제 후 해당 행은 status='purged'로 표시하고 output_path를 비운다.
(list_videos.py는 이미 DB에 있는 video_id는 건드리지 않으므로, 이후에도
이 영상들이 다시 큐에 들어오지 않는다.)

필요한 환경변수:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
"""

import os

from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
STORAGE_BUCKET = "coldsheep-piano"
CUTOFF_DATE = "2022-12-31"

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def main():
    result = (
        supabase.table("coldsheep_videos")
        .select("video_id, title, published_at, output_path")
        .lt("published_at", CUTOFF_DATE)
        .eq("status", "done")
        .execute()
    )
    rows = result.data or []
    print(f"삭제 대상: {len(rows)}개 (published_at < {CUTOFF_DATE})")

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
