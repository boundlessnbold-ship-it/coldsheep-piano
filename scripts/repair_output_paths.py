"""
폴더명(video_id)은 원복됐지만 그 안의 파일명이 수동으로 바뀐 경우를 위한 복구 스크립트.
status='done'인 각 영상의 video_id 폴더를 Storage에서 실제로 조회해서,
그 안에 있는 실제 파일명으로 output_path를 다시 맞춰준다.

- 폴더 자체가 없어진 경우(파일이 통째로 사라짐)는 자동 복구 불가 -> 로그에 남기고 건너뜀
- 폴더 안에 파일이 여러 개면 첫 번째 것을 사용 (보통 mp3 하나만 있을 것)

필요한 환경변수:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
"""

import os

from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
STORAGE_BUCKET = "coldsheep-piano"

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def main():
    result = (
        supabase.table("coldsheep_videos")
        .select("video_id, title, output_path")
        .eq("status", "done")
        .execute()
    )
    rows = result.data or []
    print(f"대상: {len(rows)}개")

    fixed = 0
    missing = []
    unchanged = 0

    for row in rows:
        video_id = row["video_id"]
        old_path = row["output_path"]

        try:
            files = supabase.storage.from_(STORAGE_BUCKET).list(video_id)
        except Exception as e:
            print(f"{video_id}: 폴더 조회 실패 - {e}")
            missing.append(video_id)
            continue

        if not files:
            print(f"{video_id}: 폴더가 비어있거나 없음")
            missing.append(video_id)
            continue

        actual_filename = files[0]["name"]
        new_path = f"{video_id}/{actual_filename}"

        if new_path == old_path:
            unchanged += 1
            continue

        supabase.table("coldsheep_videos").update(
            {"output_path": new_path}
        ).eq("video_id", video_id).execute()

        print(f"{video_id}: {old_path} -> {new_path}")
        fixed += 1

    print(f"\n완료: 수정 {fixed}개 / 변경없음 {unchanged}개 / 복구불가(폴더없음) {len(missing)}개")
    if missing:
        print("복구 불가 video_id 목록:", ", ".join(missing))


if __name__ == "__main__":
    main()
