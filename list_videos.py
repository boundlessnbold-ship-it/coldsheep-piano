"""
콜드쉽 채널의 전체 업로드 영상을 가져와 coldsheep_videos 테이블에 등록한다.
- RSS는 GitHub Actions IP에서 404 나는 걸 이미 알고 있으므로 Data API의
  playlistItems(uploads playlist)를 사용한다.
- 이미 등록된 video_id는 건드리지 않는다 (upsert on conflict do nothing) →
  이 스크립트를 daily digest 크론에 얹어서 신규 영상 자동 편입에도 재사용 가능.

필요한 환경변수:
  YOUTUBE_API_KEY
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
  COLDSHEEP_CHANNEL_ID   (채널 ID, UC로 시작. 못 구했으면 채널 핸들로 검색해서 채워넣기)
"""

import os
import sys
import time
from datetime import datetime, timezone

from googleapiclient.discovery import build
from supabase import create_client

YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
CHANNEL_ID = os.environ["COLDSHEEP_CHANNEL_ID"]  # 예: UCBN1K8xke1spkXI5e2nogYw

MAX_RETRIES = 3


def get_uploads_playlist_id(youtube, channel_id: str) -> str:
    resp = youtube.channels().list(part="contentDetails", id=channel_id).execute()
    items = resp.get("items", [])
    if not items:
        raise RuntimeError(f"채널을 찾을 수 없음: {channel_id}")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def fetch_all_uploads(youtube, uploads_playlist_id: str):
    videos = []
    page_token = None
    while True:
        for attempt in range(MAX_RETRIES):
            try:
                resp = youtube.playlistItems().list(
                    part="snippet,contentDetails",
                    playlistId=uploads_playlist_id,
                    maxResults=50,
                    pageToken=page_token,
                ).execute()
                break
            except Exception as e:
                wait = 5 * (2 ** attempt)
                print(f"[재시도 {attempt+1}/{MAX_RETRIES}] {e} -> {wait}s 대기")
                time.sleep(wait)
        else:
            raise RuntimeError("playlistItems 호출 반복 실패")

        for item in resp.get("items", []):
            snippet = item["snippet"]
            video_id = item["contentDetails"]["videoId"]
            videos.append({
                "video_id": video_id,
                "title": snippet.get("title"),
                "published_at": snippet.get("publishedAt"),
                "video_url": f"https://www.youtube.com/watch?v={video_id}",
                "status": "pending",
            })

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return videos


def main():
    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

    uploads_playlist_id = get_uploads_playlist_id(youtube, CHANNEL_ID)
    print(f"업로드 재생목록: {uploads_playlist_id}")

    videos = fetch_all_uploads(youtube, uploads_playlist_id)
    print(f"총 {len(videos)}개 영상 수집됨")

    # video_id unique 제약 기준으로 upsert, 이미 있는 건 무시 (ignore_duplicates)
    batch_size = 100
    inserted = 0
    for i in range(0, len(videos), batch_size):
        batch = videos[i:i + batch_size]
        result = supabase.table("coldsheep_videos").upsert(
            batch, on_conflict="video_id", ignore_duplicates=True
        ).execute()
        inserted += len(result.data or [])

    print(f"신규 등록: {inserted}개 (기존 영상은 건드리지 않음)")


if __name__ == "__main__":
    main()
