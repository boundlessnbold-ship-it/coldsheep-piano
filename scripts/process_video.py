"""
coldsheep_videos 테이블에서 status='pending' 인 영상을 배치로 가져와
1) 오디오만 먼저 받아서 피아노 구간 탐지
2) 피아노 구간이 없으면 영상 다운로드 없이 'no_piano' 처리 (대역폭 절약)
3) 있으면 영상 다운로드 -> 구간 컷 + 이어붙이기 -> Supabase Storage(private) 업로드
4) 상태 업데이트

필요한 환경변수:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
  BATCH_SIZE (기본 8)
  PIANO_THRESHOLD (기본 0.12)
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


def download_audio(video_url: str, out_wav: Path):
    # 오디오만 최소 용량으로 받아서 16kHz mono wav로 변환 (YAMNet 입력 스펙)
    cmd = ["yt-dlp", "-f", "bestaudio"]
    if YTDLP_PROXY:
        cmd += ["--proxy", YTDLP_PROXY]
    cmd += ["-o", str(out_wav.with_suffix(".src.%(ext)s")), video_url]
    run(cmd)
    src = next(out_wav.parent.glob(out_wav.stem + ".src.*"))
    run([
        "ffmpeg", "-y", "-i", str(src),
        "-ar", "16000", "-ac", "1", "-vn",
        str(out_wav),
    ])
    src.unlink(missing_ok=True)


def download_video(video_url: str, out_mp4: Path):
    # 720p 이하로 받아서 용량/처리시간 절약 (개인 감상용이라 화질 크게 안 중요)
    cmd = ["yt-dlp"]
    if YTDLP_PROXY:
        cmd += ["--proxy", YTDLP_PROXY]
    cmd += [
        "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "--merge-output-format", "mp4",
        "-o", str(out_mp4),
        video_url,
    ]
    run(cmd)


def get_duration_seconds(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def cut_and_concat(video_path: Path, segments: list[dict], out_path: Path, workdir: Path):
    clip_paths = []
    for i, seg in enumerate(segments):
        clip_path = workdir / f"clip_{i:03d}.mp4"
        run([
            "ffmpeg", "-y",
            "-ss", str(seg["start"]), "-to", str(seg["end"]),
            "-i", str(video_path),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac",
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
    storage_path = f"{video_id}/piano_only.mp4"
    with open(local_path, "rb") as f:
        supabase.storage.from_(STORAGE_BUCKET).upload(
            storage_path, f, {"content-type": "video/mp4", "upsert": "true"}
        )
    return storage_path


def process_one(video: dict):
    video_id = video["video_id"]
    video_url = video["video_url"]
    print(f"\n=== {video_id} 처리 시작: {video.get('title')} ===")

    supabase.table("coldsheep_videos").update(
        {"status": "processing"}
    ).eq("video_id", video_id).execute()

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        wav_path = workdir / f"{video_id}.wav"

        download_audio(video_url, wav_path)
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

        print(f"{video_id}: 피아노 구간 {len(segments)}개 발견 -> 영상 다운로드")
        video_path = workdir / f"{video_id}.mp4"
        download_video(video_url, video_path)

        duration = get_duration_seconds(video_path)
        # 영상 길이 넘는 구간 클리핑 (오디오/비디오 길이가 약간 다를 수 있음)
        for seg in segments:
            seg["end"] = min(seg["end"], duration)
        segments = [s for s in segments if s["end"] > s["start"]]

        out_path = workdir / f"{video_id}_piano.mp4"
        cut_and_concat(video_path, segments, out_path, workdir)

        storage_path = upload_to_storage(out_path, video_id)

        supabase.table("coldsheep_videos").update({
            "status": "done",
            "piano_segments": segments,
            "output_path": storage_path,
        }).eq("video_id", video_id).execute()

        print(f"{video_id}: 완료 -> {storage_path}")


def main():
    result = supabase.table("coldsheep_videos").select("*").eq(
        "status", "pending"
    ).limit(BATCH_SIZE).execute()
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
