"""Loading a local ONNX model, and refusing clearly when it cannot be.

Both AI paths are optional and both are reached the same way: an environment
variable names a model file, and if it is unset the deterministic reference
implementation runs instead. So there are exactly two ways to arrive here with
something missing, and they need different answers:

*   The **model file** is absent. The operator pointed at a path that is not
    there, and the fix is a path.
*   **ONNX Runtime** is absent. The model is fine; the machine cannot execute
    it, and the fix is an install.

Checked in that order deliberately. An air-gapped deployment installs without
``onnxruntime`` -- there is no model to run and no way to fetch one -- so a
misconfigured path there would otherwise surface as ``No module named
'onnxruntime'``, which sends the operator after the wrong problem entirely.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["load_session", "MISSING_RUNTIME"]

MISSING_RUNTIME = (
    "ONNX Runtime is not installed, so a local model cannot be executed on this "
    "machine. Either install it (pip install 'cmdm[ai]'), or unset the model "
    "variable to use the deterministic fallback, which needs no model and is "
    "what runs by default."
)


def load_session(
    model_path: str | Path,
    *,
    what: str,
    hint: str,
    intra_op_threads: int = 1,
    optimize: bool = True,
) -> tuple[Any, Any]:
    """Open an inference session, or raise something a person can act on.

    Returns ``(session, onnxruntime_module)`` -- the module comes back because
    callers need its enums, and importing it twice to get them would defeat the
    point of importing it lazily here.
    """
    path = Path(model_path)
    if not path.exists():
        raise FileNotFoundError(f"{what} not found at {path}. {hint}")

    try:
        import onnxruntime as ort
    except ImportError:
        raise RuntimeError(MISSING_RUNTIME) from None

    options = ort.SessionOptions()
    options.intra_op_num_threads = intra_op_threads
    if optimize:
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

    session = ort.InferenceSession(
        str(path), sess_options=options, providers=["CPUExecutionProvider"]
    )
    return session, ort
