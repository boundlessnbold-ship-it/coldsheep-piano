"""
coldsheep_videos 테이블에서 status='pending' 인 영상을 배치로 가져와
1) 오디오만 다운로드 (원본 화질 유지 + 별도로 YAMNet 분석용 16kHz wav 생성)
2) 피아노 구간 탐지 -> 없으면 'no_piano' 처리
3) 있으면 원본 오디오에서 그 구간만 잘라 mp3로 이어붙이기 (영상 다운로드/컷 안 함)
4) Supabase Storage(private) 업로드, 상태 업데이트

영상 대신 오디오만 남기는 이유:
- 목적이 '듣는 것'이라 영상이 필요 없음
- 결과 파일이 훨씬 작아져서 Storage 무료 플랜 파일 크기 제한(기본 50MB) 문제 회피
- yt-dlp 다운로드가 1번으로 줄어서 프록시 트래픽도 절약됨

필요한 환경변수:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
  BATCH_SIZE (기본 8)
  PIANO_THRESHOLD (기본 0.08)
  MERGE_GAP_SECONDS (기본 6.0)
  MIN_DURATION_SECONDS (기본 8.0)
  PAD_SECONDS (기본 1.0)
  YTDLP_PROXY (선택 - YouTube 봇 감지 우회용, 형식: http://user:pass@host:port)
"""

import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

from supabase import create_client

from piano_detector import detect_piano_segments

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "8"))
PIANO_THRESHOLD = float(os.environ.get("PIANO_THRESHOLD", "0.08"))
MERGE_GAP_SECONDS = float(os.environ.get("MERGE_GAP_SECONDS", "6.0"))
MIN_DURATION_SECONDS = float(os.environ.get("MIN_DURATION_SECONDS", "8.0"))
PAD_SECONDS = float(os.environ.get("PAD_SECONDS", "1.0"))
STORAGE_BUCKET = "coldsheep-piano"  # Supabase Storage에 미리 private 버킷으로 생성해둘 것

# YouTube가 GitHub Actions 데이터센터 IP를 봇으로 막는 경우가 많아서
# 기존에 쓰던 Webshare 프록시를 재사용한다.
# 형식 예: http://username:password@p.webshare.io:80
YTDLP_PROXY = os.environ.get("YTDLP_PROXY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def run(cmd: list[str]):
    print("$", " ".join(cmd))
    subprocess.run(cmd, check=True)


def download_audio_original(video_url: str, out_stub: Path) -> Path:
    """원본 화질 그대로 오디오만 다운로드. 실제 생성된 파일 경로를 반환한다."""
