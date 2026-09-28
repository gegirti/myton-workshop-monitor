"""Reliable local plot generation with a lightweight Raspberry Pi fallback."""

from __future__ import annotations

import logging
import math
from pathlib import Path

from power_logger import SessionResult, format_duration, generate_plot as matplotlib_plot

LOGGER = logging.getLogger(__name__)


def generate_plot(result: SessionResult, baseline_watts: float) -> Path:
    """Prefer matplotlib, but still produce a PNG if it is unavailable."""
    try:
        return matplotlib_plot(result, baseline_watts)
    except Exception:
        LOGGER.exception("Matplotlib plot generation failed; trying Pillow")
        return generate_pillow_plot(result, baseline_watts)


def generate_pillow_plot(result: SessionResult, baseline_watts: float) -> Path:
    """Draw a readable offline line chart using the Pi-proven Pillow stack."""
    from PIL import Image, ImageDraw, ImageFont

    samples = result.session.samples
    if not samples:
        raise ValueError("Cannot plot a session with no samples")

    width, height = 1200, 700
    left, top, right, bottom = 110, 155, 55, 95
    chart_width = width - left - right
    chart_height = height - top - bottom
    colors = {
        "background": (248, 250, 252),
        "ink": (30, 41, 59),
        "muted": (100, 116, 139),
        "grid": (203, 213, 225),
        "total": (37, 99, 235),
        "printer": (234, 88, 12),
        "peak": (190, 24, 93),
    }

    def font(size: int, bold: bool = False):
        names = (
            ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf")
            if bold
            else ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")
        )
        paths = [
            f"/usr/share/fonts/truetype/dejavu/{name}" for name in names
        ] + [f"/usr/share/fonts/{name}" for name in names]
        for path in paths:
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
        return ImageFont.load_default()

    elapsed = [float(row["elapsed_seconds"]) for row in samples]
    total = [float(row["total_watts"]) for row in samples]
    printer = [float(row["printer_watts"]) for row in samples]
    duration_seconds = max(elapsed[-1], 1.0)
    observed_max = max(total + printer + [1.0])
    y_step = max(25.0, math.ceil(observed_max / 5.0 / 25.0) * 25.0)
    y_max = y_step * 5.0

    def point(seconds: float, watts: float) -> tuple[int, int]:
        x = left + round(seconds / duration_seconds * chart_width)
        y = top + chart_height - round(watts / y_max * chart_height)
        return x, y

    image = Image.new("RGB", (width, height), colors["background"])
    draw = ImageDraw.Draw(image)
    title_font = font(34, bold=True)
    body_font = font(21)
    small_font = font(18)
    draw.text((left, 28), result.session.name, fill=colors["ink"], font=title_font)
    stats = result.statistics
    subtitle = (
        f"{format_duration(float(stats['duration_seconds'] or 0))}   |   "
        f"Printer avg {float(stats['avg_printer_watts'] or 0):.0f} W   |   "
        f"Peak {float(stats['max_printer_watts'] or 0):.0f} W"
    )
    draw.text((left, 82), subtitle, fill=colors["muted"], font=body_font)

    for index in range(6):
        watts = y_step * index
        y = top + chart_height - round(index / 5 * chart_height)
        draw.line((left, y, left + chart_width, y), fill=colors["grid"], width=1)
        draw.text(
            (left - 18, y),
            f"{watts:.0f} W",
            fill=colors["muted"],
            font=small_font,
            anchor="rm",
        )

    draw.line(
        (left, top, left, top + chart_height, left + chart_width, top + chart_height),
        fill=colors["ink"],
        width=2,
    )
    total_points = [point(t, value) for t, value in zip(elapsed, total)]
    printer_points = [point(t, value) for t, value in zip(elapsed, printer)]
    if len(samples) == 1:
        for xy, color in (
            (total_points[0], colors["total"]),
            (printer_points[0], colors["printer"]),
        ):
            draw.ellipse(
                (xy[0] - 5, xy[1] - 5, xy[0] + 5, xy[1] + 5), fill=color
            )
    else:
        draw.line(total_points, fill=colors["total"], width=5, joint="curve")
        draw.line(printer_points, fill=colors["printer"], width=5, joint="curve")

    peak_index = max(range(len(printer)), key=printer.__getitem__)
    peak_x, peak_y = printer_points[peak_index]
    draw.ellipse(
        (peak_x - 7, peak_y - 7, peak_x + 7, peak_y + 7),
        fill=colors["peak"],
    )

    total_minutes = duration_seconds / 60.0
    draw.text(
        (left, height - 68), "0", fill=colors["muted"], font=small_font, anchor="mm"
    )
    draw.text(
        (left + chart_width, height - 68),
        f"{total_minutes:.1f} min",
        fill=colors["muted"],
        font=small_font,
        anchor="mm",
    )
    legend_y = height - 28
    draw.line(
        (left, legend_y, left + 38, legend_y), fill=colors["total"], width=5
    )
    draw.text(
        (left + 50, legend_y),
        "UPS total",
        fill=colors["ink"],
        font=small_font,
        anchor="lm",
    )
    draw.line(
        (left + 190, legend_y, left + 228, legend_y),
        fill=colors["printer"],
        width=5,
    )
    draw.text(
        (left + 240, legend_y),
        "Printer estimate",
        fill=colors["ink"],
        font=small_font,
        anchor="lm",
    )
    draw.text(
        (width - right, legend_y),
        f"Baseline: {baseline_watts:g} W",
        fill=colors["muted"],
        font=small_font,
        anchor="rm",
    )

    png_path = result.session.csv_path.with_suffix(".png")
    image.save(png_path, format="PNG", optimize=True)
    return png_path
