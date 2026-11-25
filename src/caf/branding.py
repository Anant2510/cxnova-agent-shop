"""Product branding: the CX Nova Agent Orchestration Layer by Coforge; the engine underneath is the framework.

Override per deployment with CAF_PRODUCT_NAME, CAF_PRODUCT_SHORT, CAF_COMPANY and CAF_PRODUCT_TAGLINE.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Product:
    name: str = "CX Nova Agent Orchestration Layer"
    short: str = "CX Nova"
    company: str = "Coforge"
    tagline: str = "Every CX Nova agent in one place: compose them into flows, run them from your tools, on the model your policy allows."
    engine: str = "Coforge Agentic Framework"
    # Campaign Studio's palette: indigo (ai) on paper and ink
    color_primary: str = "#1f3d7a"
    color_primary_2: str = "#2c56a8"
    color_ink: str = "#17202e"
    color_paper: str = "#f3f5f7"

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


PRODUCT = Product(
    name=os.environ.get("CAF_PRODUCT_NAME", Product.name),
    short=os.environ.get("CAF_PRODUCT_SHORT", Product.short),
    company=os.environ.get("CAF_COMPANY", Product.company),
    tagline=os.environ.get("CAF_PRODUCT_TAGLINE", Product.tagline),
)

# The CX Nova mark: a node, a path and a baseline on a lacquered red tile (the portal's porcelain theme).
MARK_SVG = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><defs><linearGradient id='t' x1='0' y1='0' x2='0' y2='1'>"
            "<stop offset='0' stop-color='#f6506a'/><stop offset='1' stop-color='#c20e23'/></linearGradient>"
            "<linearGradient id='g' x1='0' y1='0' x2='0' y2='1'><stop offset='0' stop-color='#fff' stop-opacity='.42'/>"
            "<stop offset='1' stop-color='#fff' stop-opacity='0'/></linearGradient></defs>"
            "<rect width='32' height='32' rx='8' fill='url(#t)'/><path d='M0 8 Q0 0 8 0 H24 Q32 0 32 8 V13 Q16 18 0 13 Z' fill='url(#g)'/>"
            "<circle cx='9' cy='11' r='3' fill='#fff'/><path d='M9 11 L21 11 Q24 11 24 14 L24 22' stroke='#fff' stroke-width='2.4' "
            "fill='none' stroke-linecap='round'/><path d='M6 24 H26' stroke='#fff' stroke-width='2.4' stroke-linecap='round'/></svg>")
