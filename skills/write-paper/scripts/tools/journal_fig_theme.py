#!/usr/bin/env python3
"""House-style Matplotlib theme helper for APS PRL ('prl') and Nature Portfolio ('nature').

Designed to match Windows/Linux/TeX Live available fonts without falling back to DejaVu,
using pure #000000 structural ink on #FFFFFF paper and Okabe-Ito/Paul-Tol monochrome-safe cycles.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple


DIMENSIONS: Dict[str, Dict[str, float]] = {
    "prl": {
        "single": 3.375,    # 8.6 cm APS single column
        "one_half": 4.50,   # 11.4 cm
        "double": 7.008,    # 17.8 cm APS double column
        "max_height": 8.25,
    },
    "nature": {
        "single": 3.504,    # 89 mm Nature single column
        "one_half": 4.724,  # 120 mm
        "double": 7.205,    # 183 mm Nature double column
        "max_height": 9.724,# 247 mm
    },
}

# Okabe-Ito + Paul Tol colorblind-safe palette; structural ink is strictly #000000 on #FFFFFF.
PALETTE: Dict[str, str] = {
    "black": "#000000",
    "blue": "#0072B2",
    "vermilion": "#D55E00",
    "teal": "#009E73",
    "purple": "#AA3377",
    "amber": "#E69F00",
    "sky": "#56B4E9",
    "gray": "#666666",
    "light_gray": "#D9D9D9",
}

MARKER_CYCLE = ["o", "s", "^", "D", "v"]


def get_figsize(style: str = "prl", columns: str = "single", height_in: float = 2.4) -> Tuple[float, float]:
    """Return exact (width_in, height_in) for journal column geometry."""
    st = style.lower().strip()
    if st not in DIMENSIONS:
        raise ValueError(f"Unknown style '{style}'; expected 'prl' or 'nature'.")
    if columns not in ("single", "one_half", "double"):
        raise ValueError(f"Unknown columns '{columns}'; expected 'single', 'one_half', or 'double'.")
    w = DIMENSIONS[st][columns]
    h = min(float(height_in), DIMENSIONS[st]["max_height"])
    return (w, h)


def apply_house_style(style: str = "prl", usetex: bool = False) -> None:
    """Configure Matplotlib rcParams for authentic 'prl' or 'nature' publication styling."""
    import matplotlib as mpl
    import matplotlib.pyplot as plt
    from cycler import cycler

    st = style.lower().strip()
    if st not in DIMENSIONS:
        raise ValueError(f"Unknown style '{style}'; expected 'prl' or 'nature'.")

    mpl.rcdefaults()
    data_cycle = cycler(color=["#0072B2", "#D55E00", "#009E73", "#AA3377", "#666666"]) + cycler(
        linestyle=["-", "--", "-.", ":", "-"]
    )
    common: Dict[str, Any] = {
        "figure.facecolor": "#FFFFFF",
        "axes.facecolor": "#FFFFFF",
        "savefig.facecolor": "#FFFFFF",
        "savefig.dpi": 600,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "text.color": "#000000",
        "axes.labelcolor": "#000000",
        "axes.edgecolor": "#000000",
        "xtick.color": "#000000",
        "ytick.color": "#000000",
        "axes.grid": False,
        "axes.prop_cycle": data_cycle,
        "legend.frameon": False,
        "legend.borderpad": 0.2,
        "legend.handlelength": 1.4,
        "legend.handletextpad": 0.4,
        "legend.labelspacing": 0.45,
        "axes.formatter.use_mathtext": True,
    }

    if st == "prl":
        specific: Dict[str, Any] = {
            "text.usetex": usetex,
            "font.family": "serif",
            # Metric-compatible free fallbacks keep Linux/macOS builds off DejaVu (which verify_figures rejects).
            "font.serif": ["Times New Roman", "STIX Two Text", "STIXGeneral", "Liberation Serif", "Nimbus Roman", "TeX Gyre Termes"],
            "mathtext.fontset": "stix",
            "font.size": 8.0,
            "axes.labelsize": 8.5,
            "axes.titlesize": 8.5,
            "xtick.labelsize": 7.5,
            "ytick.labelsize": 7.5,
            "legend.fontsize": 7.0,
            "axes.linewidth": 0.65,
            "axes.spines.top": True,
            "axes.spines.right": True,
            "xtick.direction": "in",
            "ytick.direction": "in",
            "xtick.top": True,
            "ytick.right": True,
            "xtick.minor.visible": True,
            "ytick.minor.visible": True,
            "xtick.major.size": 3.0,
            "ytick.major.size": 3.0,
            "xtick.minor.size": 1.6,
            "ytick.minor.size": 1.6,
            "xtick.major.width": 0.60,
            "ytick.major.width": 0.60,
            "xtick.minor.width": 0.45,
            "ytick.minor.width": 0.45,
            "lines.linewidth": 1.10,
            "lines.markersize": 3.8,
            "patch.linewidth": 0.65,
        }
        if usetex:
            specific["text.latex.preamble"] = r"\usepackage{amsmath,amssymb}\usepackage{newtxtext,newtxmath}"
    else:
        specific = {
            "text.usetex": usetex,
            "font.family": "sans-serif",
            "font.sans-serif": ["Arial", "Helvetica", "Liberation Sans", "Nimbus Sans", "TeX Gyre Heros", "FreeSans"],
            "mathtext.fontset": "stixsans",
            "font.size": 6.5,
            "axes.labelsize": 7.0,
            "axes.titlesize": 7.0,
            "xtick.labelsize": 6.0,
            "ytick.labelsize": 6.0,
            "legend.fontsize": 6.0,
            "axes.linewidth": 0.55,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "xtick.direction": "out",
            "ytick.direction": "out",
            "xtick.top": False,
            "ytick.right": False,
            "xtick.minor.visible": False,
            "ytick.minor.visible": False,
            "xtick.major.size": 2.5,
            "ytick.major.size": 2.5,
            "xtick.major.width": 0.55,
            "ytick.major.width": 0.55,
            "lines.linewidth": 0.90,
            "lines.markersize": 3.2,
            "patch.linewidth": 0.55,
        }
        if usetex:
            specific["text.latex.preamble"] = r"\usepackage{helvet}\usepackage{sansmath}\sansmath"

    plt.rcParams.update({**common, **specific})


def add_panel_label(
    ax: Any,
    letter: str,
    style: str = "prl",
    x: float = -0.14,
    y: float = 1.04,
    inside: bool = False,
) -> Any:
    """Add bare journal panel identifier ('(a)' for PRL; bold 'a' for Nature) with zero title text."""
    clean = letter.strip("() ").lower()
    st = style.lower().strip()
    if st == "prl":
        px, py = (0.04, 0.94) if inside else (x, y)
        ha = "left" if inside else "right"
        return ax.text(
            px,
            py,
            f"({clean})",
            transform=ax.transAxes,
            fontsize=8.5,
            fontweight="normal",
            va="top",
            ha=ha,
            color="#000000",
        )
    return ax.text(
        x,
        y,
        clean,
        transform=ax.transAxes,
        fontsize=8.0,
        fontweight="bold",
        va="top",
        ha="right",
        color="#000000",
    )
