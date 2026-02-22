"""
Vocal separation service using audio-separator (VR Architecture).
Splits audio into vocals and background (accompaniment).
Uses Python API instead of CLI to avoid PATH issues.
Model is loaded once globally and reused across requests.
"""
import logging
import os
import shutil
import threading
from typing import Optional, Callable

logger = logging.getLogger(__name__)

# Persistent model directory (survives /tmp cleanup on reboot)
MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "models")
MODEL_NAME = "2_HP-UVR.pth"
_separator = None

# Limit threads to avoid overloading low-spec CPU
os.environ.setdefault("OMP_NUM_THREADS", "1")

# --- Progress tracking ---
# Thread-local storage for progress callback
_progress_callback: Optional[Callable[[int, int], None]] = None
_progress_lock = threading.Lock()


def init_separator():
    """Pre-load the separator model. Call at application startup."""
    global _separator
    if _separator is None:
        logger.info("Loading audio separator model (VR Arch: %s) at startup...", MODEL_NAME)
        from audio_separator.separator import Separator
        sep = Separator(
            output_format="WAV",
            model_file_dir=MODEL_DIR,
            normalization_threshold=1.0,
            vr_params={
                "batch_size": 1,             # Minimum batch — lowest memory usage
                "window_size": 1024,         # 1024=fastest, 512=default, 320=best quality
                "aggression": 5,             # Standard strength for vocal/instrumental
                "enable_tta": False,         # TTA doubles processing time
                "enable_post_process": False, # Skip artifact detection — saves CPU
                "high_end_process": False,   # Skip frequency mirroring — saves CPU
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


class _TqdmProgressHook:
    """Drop-in tqdm replacement that calls our progress callback."""

    def __init__(self, iterable=None, *args, **kwargs):
        self._iterable = iterable
        self._total = kwargs.get("total", None)
        if self._total is None and iterable is not None:
            try:
                self._total = len(iterable)
            except TypeError:
                self._total = None
        self._current = 0

    def __iter__(self):
        for item in self._iterable:
            yield item
            self._current += 1
            if _progress_callback and self._total:
                try:
                    _progress_callback(self._current, self._total)
                except Exception:
                    pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def update(self, n=1):
        self._current += n
        if _progress_callback and self._total:
            try:
                _progress_callback(self._current, self._total)
            except Exception:
                pass


def separate_audio(
    audio_path: str,
    output_dir: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """
    Separate audio into vocals and background using audio-separator Python API.

    Args:
        audio_path: Path to the input audio file (WAV).
        output_dir: Directory to store output files.
        progress_callback: Optional callback(current, total) for progress updates.

    Returns:
        dict with keys 'vocals' and 'background' pointing to output file paths.
    """
    global _progress_callback

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

    # Monkey-patch tqdm in the vr_separator module to capture progress
    import audio_separator.separator.architectures.vr_separator as vr_mod
    original_tqdm = vr_mod.tqdm
    _progress_callback = progress_callback
    vr_mod.tqdm = _TqdmProgressHook

    try:
        # Returns list of output file paths: [primary_stem, secondary_stem]
        output_files = separator.separate(audio_path)
    finally:
        # Restore original tqdm
        vr_mod.tqdm = original_tqdm
        _progress_callback = None
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
