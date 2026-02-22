"""
Vocal separation service using audio-separator (MDX-Net).
Splits audio into vocals and background (accompaniment).
Uses Python API instead of CLI to avoid PATH issues.
Model is loaded once globally and reused across requests.
"""
import logging
import os
import shutil
import threading

logger = logging.getLogger(__name__)

# Persistent model directory (survives /tmp cleanup on reboot)
MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "models")
MODEL_NAME = "UVR-MDX-NET-Inst_HQ_3.onnx"
_separator = None
_separator_lock = threading.Lock()

# Limit ONNX Runtime threads to avoid overloading CPU
os.environ.setdefault("OMP_NUM_THREADS", "4")


def init_separator():
    """Pre-load the separator model. Call at application startup."""
    global _separator
    with _separator_lock:
        if _separator is None:
            logger.info("Loading audio separator model at startup...")
            from audio_separator.separator import Separator
            sep = Separator(
                output_format="WAV",
                model_file_dir=MODEL_DIR,
                # -- Performance tuning for CPU --
                normalization_threshold=1.0,    # 设为1.0，不缩放幅度，保持原始音量
                mdx_params={
                    "hop_length": 1024,
                    "segment_size": 128,    # 默认256，减半降低内存和CPU占用
                    "overlap": 0.1,         # 默认0.25，降低重叠减少计算量
                    "batch_size": 1,
                    "enable_denoise": False,
                },
            )
            sep.load_model(model_filename=MODEL_NAME)
            _separator = sep
            logger.info("Audio separator model loaded successfully.")


def _get_separator():
    """Get the global Separator instance."""
    if _separator is None:
        init_separator()
    return _separator


def separate_audio(audio_path: str, output_dir: str) -> dict:
    """
    Separate audio into vocals and background using audio-separator Python API.

    Args:
        audio_path: Path to the input audio file (WAV).
        output_dir: Directory to store output files.

    Returns:
        dict with keys 'vocals' and 'background' pointing to output file paths.
    """
    vocals_path = os.path.join(output_dir, "vocals.wav")
    background_path = os.path.join(output_dir, "background.wav")

    # Skip if already separated
    if os.path.exists(vocals_path) and os.path.exists(background_path):
        logger.info(f"Separation already done, skipping: {output_dir}")
        return {"vocals": vocals_path, "background": background_path}

    logger.info(f"Starting vocal separation for: {audio_path}")

    separator = _get_separator()
    # Update output_dir for this request
    separator.output_dir = output_dir

    # Returns list of output file paths: [primary_stem, secondary_stem]
    output_files = separator.separate(audio_path)
    logger.info(f"Separation output files: {output_files}")

    # Find vocals and instrumental from output files
    found_vocals = None
    found_instrumental = None

    for fpath in output_files:
        fname = os.path.basename(fpath).lower()
        if "(vocals)" in fname or "vocal" in fname:
            found_vocals = fpath
        elif "(instrumental)" in fname or "instrument" in fname or "no_vocal" in fname or "accomp" in fname:
            found_instrumental = fpath

    # Fallback: if naming didn't match, scan output_dir
    if not found_vocals or not found_instrumental:
        for fname in os.listdir(output_dir):
            lower = fname.lower()
            full = os.path.join(output_dir, fname)
            if full in (vocals_path, background_path):
                continue
            if not fname.endswith(".wav"):
                continue
            if "(vocals)" in lower or "vocal" in lower:
                found_vocals = found_vocals or full
            elif "(instrumental)" in lower or "instrument" in lower or "accomp" in lower:
                found_instrumental = found_instrumental or full

    if not found_vocals or not found_instrumental:
        raise RuntimeError(
            f"Could not find separation output files in {output_dir}. "
            f"Files present: {os.listdir(output_dir)}"
        )

    # Rename to standard names
    if found_vocals != vocals_path:
        shutil.move(found_vocals, vocals_path)
    if found_instrumental != background_path:
        shutil.move(found_instrumental, background_path)

    logger.info(f"Separation complete: vocals={vocals_path}, background={background_path}")
    return {"vocals": vocals_path, "background": background_path}
