"""
이미 status='done'으로 완료됐지만 song_guess가 비어있는 기존 mp3들에 대해
Storage에서 mp3를 다시 받아 Gemini로 곡명을 추정하고 채워넣는 1회성 백필 스크립트.

process_video.py의 파이프라인과 별개로, 신규 다운로드/컷 작업 없이
이미 만들어진 결과물만 대상으로 한다.

필요한 환경변수:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
  GEMINI_API_KEY, GEMINI_MODEL(선택, 기본 gemini-2.0-flash)
  BACKFILL_LIMIT (선택, 기본 50 - 한 번에 처리할 최대 개수)
"""

import base64
import os
import tempfile
import time
import traceback
from pathlib import Path

import requests
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
BACKFILL_LIMIT = int(os.environ.get("BACKFILL_LIMIT", "50"))
STORAGE_BUCKET = "coldsheep-piano"

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def guess_song(mp3_path: Path, video_title: str) -> str | None:
    with open(mp3_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode("utf-8")

    prompt = (
        "이 오디오는 길거리 피아니스트가 즉흥적으로 연주한 클립입니다. "
        "전곡이 아니라 일부만 발췌됐거나, 편곡·즉흥연주라서 원곡과 다르게 들릴 수 있고, "
        "여러 곡이 이어서 연주된 메들리일 수도 있습니다. "
        f"참고로 원본 영상 제목은 '{video_title}' 입니다. "
        "이 연주에 포함된 곡을 모두 찾아서 각각 '아티스트 - 곡명' 형식으로 답하되, "
        "여러 곡이면 쉼표(,)로 구분해서 나열하세요 (예: '아티스트1 - 곡명1, 아티스트2 - 곡명2'). "
        "확신이 없는 곡은 포함하지 말고, 확실한 것만 답하세요. "
        "하나도 확신이 없으면 '추정 불가' 라고만 답하세요. "
        "설명이나 부연 설명은 절대 붙이지 마세요."
    )

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "audio/mpeg", "data": audio_b64}},
            ]
        }]
    }

    resp = requests.post(url, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        return None


def main():
    result = (
        supabase.table("coldsheep_videos")
        .select("video_id, title, output_path")
        .eq("status", "done")
        .is_("song_guess", "null")
        .not_.is_("output_path", "null")
        .limit(BACKFILL_LIMIT)
        .execute()
    )
    rows = result.data or []

    if not rows:
        print("곡명 추정 안 된 완료 항목 없음 (다 채워졌거나 output_path 없음)")
        return

    print(f"백필 대상: {len(rows)}개")

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        for row in rows:
            video_id = row["video_id"]
            output_path = row["output_path"]
            print(f"\n=== {video_id}: {row.get('title')} ===")
            try:
                mp3_bytes = supabase.storage.from_(STORAGE_BUCKET).download(output_path)
                local_mp3 = workdir / f"{video_id}.mp3"
                local_mp3.write_bytes(mp3_bytes)

                song_guess = guess_song(local_mp3, row.get("title") or "")
                print(f"{video_id}: 추정 -> {song_guess}")

                supabase.table("coldsheep_videos").update(
                    {"song_guess": song_guess}
                ).eq("video_id", video_id).execute()

                local_mp3.unlink(missing_ok=True)
                time.sleep(1)  # Gemini 호출 간 살짝 텀 (레이트리밋 여유)
            except Exception as e:
                print(f"{video_id}: 실패 - {e}")
                traceback.print_exc()


if __name__ == "__main__":
    main()
