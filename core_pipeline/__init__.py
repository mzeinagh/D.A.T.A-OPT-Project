"""Framework-agnostic processing, retrieval, and classification logic ported
from the original DATA_Project CLI pipeline.

Nothing in this package imports Django. It is called *from* the `studies`
app's services/tasks, which translate between Django models and the plain
dicts / `GraphState` this package already works with. Keep it that way —
that separation is the whole point of the Django conversion.
"""
