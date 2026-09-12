"""Batched frame math for the AST model's existing 16 kHz input features."""
import numpy as np


def ast_features(audio, feature):
    # Match transformers.audio_utils.spectrogram's precision, FFT rounding,
    # DC removal and preemphasis. Only frames retained by AST are computed.
    # Each frame is independent; this replaces its Python loop, not its math.
    frames = np.lib.stride_tricks.sliding_window_view(
        audio.astype(np.float64), 400)[::160][:1024].copy()
    frames -= frames.mean(axis=1, keepdims=True)
    frames[:, 1:] -= .97 * frames[:, :-1]
    frames[:, 0] *= 1 - .97
    frames *= feature.window.astype(np.float64)
    spectrum = np.fft.rfft(frames, n=512, axis=1).astype(np.complex64)
    power = np.abs(spectrum, dtype=np.float64) ** 2.
    fb = np.log(np.maximum(1.192092955078125e-7,
        np.dot(feature.mel_filters.T, power.T))).astype(np.float32).T
    fb = np.pad(fb, ((0, 1024-len(fb)), (0, 0)))
    return feature.normalize(fb).astype(np.float32)
