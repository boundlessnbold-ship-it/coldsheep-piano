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

import base64
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

import requests
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

# 곡명 추정용 Gemini. 메인 파이프라인 쿼터(하루 250회)와 별개로 쓰려면
# 여기엔 별도 발급받은 키를 넣는 걸 추천 (기존에 test repo용 키 쓰던 패턴과 동일).
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def run(cmd: list[str]):
    print("$", " ".join(cmd))
    subprocess.run(cmd, check=True)


def download_audio_original(video_url: str, out_stub: Path) -> Path:
    """원본 화질 그대로 오디오만 다운로드. 실제 생성된 파일 경로를 반환한다."""
    cmd = ["yt-dlp", "-f", "bestaudio"]
    if YTDLP_PROXY:
        cmd += ["--proxy", YTDLP_PROXY]
    cmd += ["-o", str(out_stub) + ".%(ext)s", video_url]
    run(cmd)
    matches = list(out_stub.parent.glob(out_stub.name + ".*"))
    if not matches:
        raise RuntimeError("오디오 다운로드 결과 파일을 찾을 수 없음")
    return matches[0]


def make_analysis_wav(src_path: Path, out_wav: Path):
    """YAMNet 입력 스펙(16kHz mono)에 맞춘 분석용 wav 생성 (원본은 그대로 둠)."""
    run([
        "ffmpeg", "-y", "-i", str(src_path),
        "-ar", "16000", "-ac", "1", "-vn",
        str(out_wav),
    ])


def get_duration_seconds(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def cut_and_concat_audio(src_path: Path, segments: list[dict], out_path: Path, workdir: Path):
    clip_paths = []
    for i, seg in enumerate(segments):
        clip_path = workdir / f"clip_{i:03d}.mp3"
        run([
            "ffmpeg", "-y",
            "-ss", str(seg["start"]), "-to", str(seg["end"]),
            "-i", str(src_path),
            "-vn", "-acodec", "libmp3lame", "-b:a", "128k",
            "-avoid_negative_ts", "make_zero",
            str(clip_path),
        ])
        clip_paths.append(clip_path)

    list_file = workdir / "concat_list.txt"
    list_file.write_text("\n".join(f"file '{p.name}'" for p in clip_paths))

    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        str(out_path),
    ])


def upload_to_storage(local_path: Path, video_id: str) -> str:
    storage_path = f"{video_id}/piano_only.mp3"
    with open(local_path, "rb") as f:
        supabase.storage.from_(STORAGE_BUCKET).upload(
            storage_path, f, {"content-type": "audio/mpeg", "upsert": "true"}
        )
    return storage_path


def guess_song(mp3_path: Path, video_title: str) -> str | None:
    """Gemini에 오디오를 들려주고 원곡을 추정시킨다. 실패/키없음이면 None 반환."""
    if not GEMINI_API_KEY:
        return None

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


def process_one(video: dict):
    video_id = video["video_id"]
    video_url = video["video_url"]
    print(f"\n=== {video_id} 처리 시작: {video.get('title')} ===")

    supabase.table("coldsheep_videos").update(
        {"status": "processing"}
    ).eq("video_id", video_id).execute()

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        src_stub = workdir / video_id
        wav_path = workdir / f"{video_id}.wav"

        src_path = download_audio_original(video_url, src_stub)
        make_analysis_wav(src_path, wav_path)

        segments = detect_piano_segments(
            str(wav_path),
            threshold=PIANO_THRESHOLD,
            merge_gap_seconds=MERGE_GAP_SECONDS,
            min_duration_seconds=MIN_DURATION_SECONDS,
            pad_seconds=PAD_SECONDS,
        )

        if not segments:
            print(f"{video_id}: 피아노 구간 없음")
            supabase.table("coldsheep_videos").update(
                {"status": "no_piano", "piano_segments": []}
            ).eq("video_id", video_id).execute()
            return

        duration = get_duration_seconds(src_path)
        for seg in segments:
            seg["end"] = min(seg["end"], duration)
        segments = [s for s in segments if s["end"] > s["start"]]

        print(f"{video_id}: 피아노 구간 {len(segments)}개 발견 -> 오디오 컷/합치기")
        out_path = workdir / f"{video_id}_piano.mp3"
        cut_and_concat_audio(src_path, segments, out_path, workdir)

        storage_path = upload_to_storage(out_path, video_id)

        song_guess = None
        try:
            song_guess = guess_song(out_path, video.get("title") or "")
        except Exception as e:
            print(f"{video_id}: 곡명 추정 실패 (무시하고 진행) - {e}")

        supabase.table("coldsheep_videos").update({
            "status": "done",
            "piano_segments": segments,
            "output_path": storage_path,
            "song_guess": song_guess,
        }).eq("video_id", video_id).execute()

        print(f"{video_id}: 완료 -> {storage_path} (추정곡: {song_guess})")


def main():
    result = (
        supabase.table("coldsheep_videos")
        .select("*")
        .eq("status", "pending")
        .order("published_at", desc=True)  # 최신 업로드 영상부터 우선 처리
        .limit(BATCH_SIZE)
        .execute()
    )
    videos = result.data or []

    if not videos:
        print("처리할 pending 영상 없음")
        return

    print(f"이번 배치: {len(videos)}개")
    for video in videos:
        try:
            process_one(video)
        except Exception as e:
            print(f"{video['video_id']} 실패: {e}")
            traceback.print_exc()
            supabase.table("coldsheep_videos").update({
                "status": "failed",
                "error_message": str(e)[:500],
            }).eq("video_id", video["video_id"]).execute()


if __name__ == "__main__":
    main()
