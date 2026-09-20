"""
내가 지정한 유튜브 링크(들)만 대상으로 피아노 연주 구간을 추출해서 mp3로 변환,
Supabase Storage에 업로드하고 DB에 기록한다.

채널 전체를 스캔하는 list_videos.py / process_video.py 자동 파이프라인과는 별개로,
링크를 직접 지정해서 그 영상들만 즉시 처리하고 싶을 때 사용.

이미 coldsheep_videos 테이블에 있는 영상이든 없든 상관없이 동작한다.
- 없으면 새로 행을 만들고
- 있으면 상태를 'processing'으로 바꿔서 재처리한다 (done/purged/failed 여부 무관하게 강제 재처리)
자동 스캔(list_videos.py)이 나중에 같은 영상을 다시 만나도, 이미 존재하는 video_id는
upsert(ignore_duplicates=True)라서 건드리지 않는다 -> 중복 처리 걱정 없음.

필요한 환경변수:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
  TARGET_URLS        처리할 유튜브 링크. 줄바꿈(\n) 또는 쉼표(,)로 여러 개 구분 가능
  PIANO_THRESHOLD     (기본 0.08)
  MERGE_GAP_SECONDS   (기본 6.0)
  MIN_DURATION_SECONDS (기본 8.0)
  PAD_SECONDS         (기본 1.0)
  CONTINUATION_RATIO  (기본 0.5)
  YTDLP_PROXY         (선택, Webshare 프록시 URL: http://user:pass@host:port)
  GEMINI_API_KEY      (선택, 없으면 song_guess 없이 진행)
  GEMINI_MODEL        (기본 gemini-2.5-flash)

사용 예 (GitHub Actions workflow_dispatch input 등에서):
  TARGET_URLS="https://youtu.be/AAAA,https://youtu.be/BBBB"
"""
import os
import re
import subprocess
import traceback

from supabase import create_client

from piano_detector import detect_piano_segments

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
STORAGE_BUCKET = "coldsheep-piano"

TARGET_URLS_RAW = os.environ["TARGET_URLS"]

PIANO_THRESHOLD = float(os.environ.get("PIANO_THRESHOLD", "0.08"))
MERGE_GAP_SECONDS = float(os.environ.get("MERGE_GAP_SECONDS", "6.0"))
MIN_DURATION_SECONDS = float(os.environ.get("MIN_DURATION_SECONDS", "8.0"))
PAD_SECONDS = float(os.environ.get("PAD_SECONDS", "1.0"))
CONTINUATION_RATIO = float(os.environ.get("CONTINUATION_RATIO", "0.5"))

YTDLP_PROXY = os.environ.get("YTDLP_PROXY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def parse_target_urls(raw: str):
    parts = re.split(r"[\n,]+", raw)
    return [p.strip() for p in parts if p.strip()]


def extract_video_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    raise ValueError(f"video_id를 추출할 수 없는 URL: {url}")


def run(cmd):
    print("+", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)


YTDLP_CLIENT_ARGS = ["--extractor-args", "youtube:player_client=android,-web"]


def download_audio_original(video_url: str, out_stub: str) -> str:
    cmd = ["yt-dlp", "-f", "bestaudio"] + YTDLP_CLIENT_ARGS
    if YTDLP_PROXY:
        cmd += ["--proxy", YTDLP_PROXY]
    cmd += ["-o", f"{out_stub}.%(ext)s", video_url]
    run(cmd)
    for f in os.listdir("."):
        if f.startswith(os.path.basename(out_stub) + "."):
            return f
    raise FileNotFoundError(f"다운로드된 오디오를 찾을 수 없음: {out_stub}")


def fetch_video_title(video_url: str) -> str:
    cmd = ["yt-dlp", "--print", "%(title)s", "--skip-download"] + YTDLP_CLIENT_ARGS
    if YTDLP_PROXY:
        cmd += ["--proxy", YTDLP_PROXY]
    cmd += [video_url]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result.stdout.strip()


def make_analysis_wav(src_path: str, out_wav: str):
    run(["ffmpeg", "-y", "-i", src_path, "-ac", "1", "-ar", "16000", out_wav])


def get_duration_seconds(path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        check=True, capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def cut_and_concat_audio(src_path: str, segments, out_path: str, workdir: str) -> str:
    parts = []
    for i, seg in enumerate(segments):
        part_path = os.path.join(workdir, f"part_{i}.mp3")
        run([
            "ffmpeg", "-y", "-ss", str(seg["start"]), "-to", str(seg["end"]),
            "-i", src_path, "-acodec", "libmp3lame", "-b:a", "128k", part_path,
        ])
        parts.append(part_path)

    list_path = os.path.join(workdir, "concat_list.txt")
    with open(list_path, "w") as f:
        for p in parts:
            f.write(f"file '{os.path.abspath(p)}'\n")

    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
         "-acodec", "libmp3lame", "-b:a", "128k", out_path])
    return out_path


def upload_to_storage(local_path: str, video_id: str) -> str:
    storage_path = f"{video_id}/piano_only.mp3"
    with open(local_path, "rb") as f:
        supabase.storage.from_(STORAGE_BUCKET).upload(
            storage_path, f, {"content-type": "audio/mpeg", "upsert": "true"},
        )
    return storage_path


def guess_song(mp3_path: str, video_title: str):
    if not GEMINI_API_KEY:
        return None
    import base64
    import requests

    with open(mp3_path, "rb") as f:
        audio_b64 = base64.b64encode(f.read()).decode()

    prompt = (
        f"이 오디오는 유튜브 영상 '{video_title}'에서 피아노 연주 부분만 발췌한 것입니다. "
        "연주되고 있는 곡의 제목을 '아티스트 - 곡명' 형식으로 알려주세요. "
        "메들리처럼 여러 곡이면 쉼표로 구분해서 모두 알려주세요. "
        "확신할 수 없으면 '추정 불가'라고만 답하세요. 다른 설명은 추가하지 마세요."
    )
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "audio/mp3", "data": audio_b64}},
            ]
        }]
    }
    resp = requests.post(url, json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        return None


def upsert_video_row(video_id: str, video_url: str, title: str):
    existing = (
        supabase.table("coldsheep_videos")
        .select("video_id")
        .eq("video_id", video_id)
        .execute()
    )
    if existing.data:
        supabase.table("coldsheep_videos").update(
            {"status": "processing", "video_url": video_url, "title": title,
             "error_message": None}
        ).eq("video_id", video_id).execute()
    else:
        supabase.table("coldsheep_videos").insert(
            {"video_id": video_id, "video_url": video_url, "title": title,
             "status": "processing"}
        ).execute()


def process_one(video_url: str):
    video_id = extract_video_id(video_url)
    print(f"\n=== {video_id} ({video_url}) 처리 시작 ===")

    try:
        title = fetch_video_title(video_url)
    except Exception:
        title = video_id

    upsert_video_row(video_id, video_url, title)

    workdir = f"work_{video_id}"
    os.makedirs(workdir, exist_ok=True)
    prev_cwd = os.getcwd()
    try:
        os.chdir(workdir)

        stub = "audio_original"
        audio_path = download_audio_original(video_url, stub)

        wav_path = "analysis.wav"
        make_analysis_wav(audio_path, wav_path)

        duration = get_duration_seconds(audio_path)

        segments = detect_piano_segments(
            wav_path,
            threshold=PIANO_THRESHOLD,
            continuation_ratio=CONTINUATION_RATIO,
            merge_gap_seconds=MERGE_GAP_SECONDS,
            min_duration_seconds=MIN_DURATION_SECONDS,
            pad_seconds=PAD_SECONDS,
            video_duration_seconds=duration,
        )

        if not segments:
            print(f"{video_id}: 피아노 구간 없음")
            os.chdir(prev_cwd)
            supabase.table("coldsheep_videos").update(
                {"status": "no_piano", "piano_segments": []}
            ).eq("video_id", video_id).execute()
            return

        out_mp3 = "piano_only.mp3"
        cut_and_concat_audio(audio_path, segments, out_mp3, ".")

        storage_path = upload_to_storage(out_mp3, video_id)

        song_guess = None
        try:
            song_guess = guess_song(out_mp3, title)
        except Exception as e:
            print(f"{video_id}: 곡 추정 실패 (무시하고 진행) - {e}")

        os.chdir(prev_cwd)

        update_payload = {
            "status": "done",
            "piano_segments": segments,
            "output_path": storage_path,
        }
        if song_guess:
            update_payload["song_guess"] = song_guess

        supabase.table("coldsheep_videos").update(update_payload).eq(
            "video_id", video_id
        ).execute()
        print(f"{video_id}: 완료 -> {storage_path}")

    except Exception as e:
        os.chdir(prev_cwd)
        print(f"{video_id}: 실패 - {e}")
        traceback.print_exc()
        supabase.table("coldsheep_videos").update(
            {"status": "failed", "error_message": str(e)}
        ).eq("video_id", video_id).execute()


def main():
    urls = parse_target_urls(TARGET_URLS_RAW)
    print(f"처리할 링크: {len(urls)}개")
    for url in urls:
        process_one(url)
    print("\n전체 완료")


if __name__ == "__main__":
    main()
