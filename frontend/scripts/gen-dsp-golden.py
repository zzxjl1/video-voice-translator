"""
Generate golden vectors for the browser MDX-Net DSP port.

Reproduces EXACTLY what audio-separator does in
`audio_separator/separator/architectures/mdx_separator.py` +
`uvr_lib_v5/stft.py`, using torch, so the JavaScript implementation
(`frontend/utils/mdx/mdxDsp.js`) can be validated numerically in Node.

Run from `frontend/`:
    python3 scripts/gen-dsp-golden.py

Writes raw little-endian float32 files to /tmp/mdx_golden/.
Requires torch (already available on the dev machine).
"""
import os
import numpy as np
import torch

OUT = "/tmp/mdx_golden"
os.makedirs(OUT, exist_ok=True)

# Parameters used by UVR-MDX-NET-Inst_HQ_3 (dim_f=3072, dim_t=256 in the ONNX graph)
CANDIDATES = [
    {"tag": "nfft6144", "n_fft": 6144, "dim_f": 3072},
    {"tag": "nfft7680", "n_fft": 7680, "dim_f": 3072},
]

SR = 44100
DUR = 3.0
N = int(SR * DUR)
N_CH = 2


def dump(name, arr):
    arr = np.ascontiguousarray(arr, dtype=np.float32)
    path = os.path.join(OUT, name)
    arr.tofile(path)
    print(f"  wrote {name:34} shape={arr.shape} bytes={arr.size * 4}")


def stft_uvr(mix: torch.Tensor, n_fft: int, hop: int, dim_f: int) -> torch.Tensor:
    """audio-separator uvr_lib_v5 STFT.__call__, input (B, C, T) -> (B, C*2, dim_f, Tf)."""
    window = torch.hann_window(window_length=n_fft, periodic=True)
    batch = mix.shape[:-2]
    ch, t = mix.shape[-2:]
    flat = mix.reshape(-1, t)

    out = torch.stft(
        flat, n_fft=n_fft, hop_length=hop, window=window, center=True, return_complex=False
    )  # (B*C, F, Tf, 2)
    permuted = out.permute([0, 3, 1, 2])            # (B*C, 2, F, Tf)
    tmp = permuted.reshape([*batch, ch, 2, -1, permuted.shape[-1]])
    final = tmp.reshape([*batch, ch * 2, -1, permuted.shape[-1]])
    return final[..., :dim_f, :], window


def istft_uvr(spec: torch.Tensor, n_fft: int, hop: int, window: torch.Tensor) -> torch.Tensor:
    """audio-separator uvr_lib_v5 STFT.inverse, input (B, C*2, dim_f, Tf) -> (B, 2, T)."""
    batch = spec.shape[:-3]
    ch2, freq_dim, time_dim = spec.shape[-3:]
    n_bins = n_fft // 2 + 1

    pad = torch.zeros([*batch, ch2, n_bins - freq_dim, time_dim], dtype=spec.dtype)
    padded = torch.cat([spec, pad], dim=-2)

    reshaped = padded.reshape([*batch, ch2 // 2, 2, n_bins, time_dim])
    flattened = reshaped.reshape([-1, 2, n_bins, time_dim])
    permuted = flattened.permute([0, 2, 3, 1])
    complex_tensor = torch.complex(permuted[..., 0], permuted[..., 1])

    result = torch.istft(
        complex_tensor, n_fft=n_fft, hop_length=hop, window=window, center=True
    )
    return result.reshape([*batch, 2, -1])


def main():
    rng = np.random.RandomState(20261003)

    # Deterministic stereo test signal: tones + noise, plausible dynamic range.
    t = np.arange(N, dtype=np.float32) / SR
    left = (
        0.4 * np.sin(2 * np.pi * 220.0 * t)
        + 0.2 * np.sin(2 * np.pi * 660.0 * t)
        + 0.05 * rng.standard_normal(N).astype(np.float32)
    ).astype(np.float32)
    right = (
        0.35 * np.sin(2 * np.pi * 330.0 * t)
        + 0.15 * np.sin(2 * np.pi * 990.0 * t)
        + 0.05 * rng.standard_normal(N).astype(np.float32)
    ).astype(np.float32)

    dump("input_left.f32", left)
    dump("input_right.f32", right)

    mix = torch.from_numpy(np.stack([left, right])[None, ...])  # (1, 2, T)

    for cand in CANDIDATES:
        n_fft, dim_f = cand["n_fft"], cand["dim_f"]
        hop = n_fft // 4
        tag = cand["tag"]
        print(f"\n[{tag}] n_fft={n_fft} hop={hop} dim_f={dim_f}")

        spec, window = stft_uvr(mix, n_fft, hop, dim_f)
        spec_np = spec.numpy()[0]  # (4, dim_f, Tf)
        print(f"  spec shape={spec_np.shape}")
        dump(f"{tag}_spec_re_im.f32", spec_np)  # channel order [c0re, c0im, c1re, c1im]

        rec = istft_uvr(spec, n_fft, hop, window)
        dump(f"{tag}_istft_L.f32", rec.numpy()[0, 0])
        dump(f"{tag}_istft_R.f32", rec.numpy()[0, 1])
        print(f"  istft length={rec.shape[-1]} (input={N})")

        # Sanity: does the round-trip reproduce the input inside the valid range?
        rec_l = rec.numpy()[0, 0]
        m = min(len(rec_l), N)
        err = np.abs(rec_l[:m] - left[:m]).max()
        print(f"  torch stft->istft round-trip max abs error = {err:.6e}")

    # Also dump the exact chunking/padding plan for the 256-frame segment size,
    # so the JS overlap-add can be compared step by step.
    for cand in CANDIDATES:
        n_fft = cand["n_fft"]
        hop = n_fft // 4
        segment_size = 256
        trim = n_fft // 2
        chunk_size = hop * (segment_size - 1)
        gen_size = chunk_size - 2 * trim
        T = 100000  # arbitrary short slice to test padding math
        pad = gen_size + trim - (T % gen_size)
        print(
            f"\n[{cand['tag']}] chunk plan: hop={hop} trim={trim} "
            f"chunk_size={chunk_size} gen_size={gen_size} "
            f"pad({T})={pad} mixture_len={trim + T + pad}"
        )
        with open(os.path.join(OUT, f"{cand['tag']}_plan.txt"), "w") as f:
            f.write(
                f"hop {hop}\ntrim {trim}\nchunk_size {chunk_size}\n"
                f"gen_size {gen_size}\nsegment_size {segment_size}\n"
            )

    print(f"\nGolden vectors written to {OUT}")


if __name__ == "__main__":
    main()
