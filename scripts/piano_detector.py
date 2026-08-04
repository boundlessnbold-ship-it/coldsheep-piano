"""
YAMNet(AudioSet 521 클래스 오디오 분류 모델)으로 wav 파일에서
'피아노' 클래스 점수가 높은 구간만 골라 시간 구간 리스트로 반환한다.

YAMNet 스펙: 16kHz mono 입력, 프레임 hop 0.48s / window 0.96s.
"""

import csv
import io

import numpy as np
import scipy.io.wavfile as wavfile
import tensorflow as tf
import tensorflow_hub as hub

_MODEL = None
_CLASS_NAMES = None

HOP_SECONDS = 0.48
WINDOW_SECONDS = 0.96

# 튜닝 파라미터 (필요하면 process_video.py에서 오버라이드)
# 길거리 피아노는 주변 소음(말소리/박수/환호) 때문에 YAMNet의 순간 점수가
# 연주 중간중간 뚝뚝 떨어지기 쉬워서, 기본값을 관대하게 잡았다.
DEFAULT_THRESHOLD = 0.08
DEFAULT_MERGE_GAP_SECONDS = 6.0
DEFAULT_MIN_DURATION_SECONDS = 8.0
DEFAULT_PAD_SECONDS = 1.0

# 소음 환경에서 "Piano" 단일 클래스만 보면 자주 놓치므로,
# 관련 클래스 중 최댓값을 사용한다.
TARGET_CLASSES = ["Piano", "Keyboard (musical)"]

# 히스테리시스: threshold를 넘는 '확실한' 구간을 먼저 찾고, 그 앞뒤로는
# 더 낮은 기준(threshold * CONTINUATION_RATIO)까지 계속 이어지는 한 확장한다.
# 조용한 솔로 도입부가 확실한(보컬 합류 등) 구간 바로 앞에 붙어있을 때
# 같이 살아남게 하기 위함 - 단일 고정 threshold로는 이게 잘려나갔다.
DEFAULT_CONTINUATION_RATIO = 0.5


def _load_model():
    global _MODEL, _CLASS_NAMES
    if _MODEL is None:
        _MODEL = hub.load("https://tfhub.dev/google/yamnet/1")
        class_map_path = _MODEL.class_map_path().numpy().decode("utf-8")
        with tf.io.gfile.GFile(class_map_path) as f:
            _CLASS_NAMES = [row["display_name"] for row in csv.DictReader(f)]
    return _MODEL, _CLASS_NAMES


def _load_wav_16k_mono(path: str) -> np.ndarray:
    sr, data = wavfile.read(path)
    if sr != 16000:
        raise ValueError(f"wav가 16kHz가 아님 (실제 {sr}Hz) - ffmpeg 변환 옵션 확인 필요")
    if data.dtype == np.int16:
        data = data.astype(np.float32) / 32768.0
    elif data.dtype != np.float32:
        data = data.astype(np.float32)
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data


def detect_piano_segments(
    wav_path: str,
    threshold: float = DEFAULT_THRESHOLD,
    continuation_ratio: float = DEFAULT_CONTINUATION_RATIO,
    merge_gap_seconds: float = DEFAULT_MERGE_GAP_SECONDS,
    min_duration_seconds: float = DEFAULT_MIN_DURATION_SECONDS,
    pad_seconds: float = DEFAULT_PAD_SECONDS,
    video_duration_seconds: float | None = None,
):
    """반환값: [{"start": float, "end": float}, ...] (초 단위, 이미 병합/필터/패딩 완료)"""
    model, class_names = _load_model()
    waveform = _load_wav_16k_mono(wav_path)

    scores, embeddings, spectrogram = model(waveform)
    scores = scores.numpy()  # (num_frames, 521)

    piano_idx = [class_names.index(c) for c in TARGET_CLASSES]
    # 여러 클래스 중 프레임별 최댓값을 사용 (Piano 단일 클래스보다 소음에 안정적)
    piano_scores = scores[:, piano_idx].max(axis=1)

    is_strict = piano_scores > threshold
    is_loose = piano_scores > (threshold * continuation_ratio)
    n = len(is_strict)

    # 1) threshold를 넘는 '확실한' 구간을 프레임 단위로 찾는다
    # 2) 그 구간의 시작/끝을, loose 기준을 만족하는 한 계속 앞/뒤로 넓힌다
    #    -> 확실한 구간 바로 앞뒤에 붙은 조용한 부분이 같이 살아남음
    raw_segments = []
    i = 0
    while i < n:
        if is_strict[i]:
            start = i
            while i < n and is_strict[i]:
                i += 1
            end = i  # exclusive

            s = start
            while s > 0 and is_loose[s - 1]:
                s -= 1
            e = end
            while e < n and is_loose[e]:
                e += 1

            raw_segments.append((s * HOP_SECONDS, e * HOP_SECONDS + WINDOW_SECONDS))
        else:
            i += 1

    if not raw_segments:
        return []

    # gap 이내 구간 병합
    merged = [list(raw_segments[0])]
    for s, e in raw_segments[1:]:
        if s - merged[-1][1] <= merge_gap_seconds:
            merged[-1][1] = e
        else:
            merged.append([s, e])

    # 최소 길이 필터 + 패딩
    final = []
    for s, e in merged:
        if e - s < min_duration_seconds:
            continue
        s = max(0.0, s - pad_seconds)
        e = e + pad_seconds
        if video_duration_seconds is not None:
            e = min(e, video_duration_seconds)
        final.append({"start": round(s, 2), "end": round(e, 2)})

    return final
