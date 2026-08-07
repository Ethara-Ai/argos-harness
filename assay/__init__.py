"""ASSAY - process verification for Milo-Bench (Aurora).

Two channels grade *how* a run solved a task, alongside the existing outcome
verifier which grades *whether* the repository ended up correct:

  Channel A (deterministic)  assay.checks   - pytest over bytes on disk
  Channel B (judged)         assay.rubric   - LLM council over natural-language items

The split is a decidability partition, not a topic split. See
``.work/ASSAY-methodology.md`` section 3.2.
"""

from .atif import Step, ToolCall, Trajectory
from .bundle import RunBundle, TaskBundle


__all__ = ["Step", "ToolCall", "Trajectory", "RunBundle", "TaskBundle"]
__version__ = "0.1.0"
