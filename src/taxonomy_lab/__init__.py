"""馆藏标本事件结构化观察材料的基础组件。"""

from .contracts import EvidenceItem, EvidenceProtocol, ValidationError
from .analysis import ALGORITHM_VERSION, analyze, bootstrap_mean_interval
from .numeric import NumericSummary, WilsonInterval
from .service import TaxonomyLabService

__all__ = [
    "NumericSummary",
    "EvidenceItem",
    "EvidenceProtocol",
    "ValidationError",
    "WilsonInterval",
    "ALGORITHM_VERSION",
    "TaxonomyLabService",
    "analyze",
    "bootstrap_mean_interval",
]

__version__ = "0.1.0"
