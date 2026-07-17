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
DEFAULT_THRESHOLD = 0.12
DEFAULT_MERGE_GAP_SECONDS = 3.0
DEFAULT_MIN_DURATION_SECONDS = 15.0
DEFAULT_PAD_SECONDS = 1.0


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

    piano_idx = class_names.index("Piano")
    # 참고: "Keyboard (musical)"도 있음. 콜드쉽 영상이 어쿠스틱 피아노 위주라
    # Piano 단일 클래스로 시작하고, 오탐/누락이 많으면 두 클래스 max로 바꿀 것.
    piano_scores = scores[:, piano_idx]

    is_piano = piano_scores > threshold

    # 프레임 -> 원시 구간
    raw_segments = []
    start = None
    for i, flag in enumerate(is_piano):
        t = i * HOP_SECONDS
        if flag and start is None:
            start = t
        elif not flag and start is not None:
            raw_segments.append((start, t + WINDOW_SECONDS))
            start = None
    if start is not None:
        raw_segments.append((start, len(is_piano) * HOP_SECONDS + WINDOW_SECONDS))

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
