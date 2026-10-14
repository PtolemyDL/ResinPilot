"""Chitubox resin profile (.cfg) export."""

from __future__ import annotations

import re
from pathlib import Path

from .models import Printer, PrintSettings, Resin


def sanitize_filename_part(text: str) -> str:
    text = text.replace(" ", "_")
    return re.sub(r"[^\w.\-]+", "", text)


def default_cfg_filename(
    printer: Printer,
    resin: Resin,
    layer_height: float,
    vat_temp_c: float,
) -> str:
    return (
        f"{sanitize_filename_part(printer.name)}_"
        f"{sanitize_filename_part(resin.name)}_"
        f"{layer_height:.3f}mm_"
        f"{vat_temp_c:.0f}C.cfg"
    )


def build_cfg_lines(
    printer: Printer,
    resin: Resin,
    settings: PrintSettings,
    profile_name: str | None = None,
) -> list[str]:
    name = profile_name or f"{resin.name} {settings.layer_height:.3f}mm"
    # Chitubox uses both historical and modern key names; write modern forms
    # plus commonly expected aliases where versions differ.
    pairs: list[tuple[str, str]] = [
        ("__resin", "normal"),
        ("antiAliasLevel", "0"),
        ("bAntiAliasLevelEditable", "1"),
        ("bAntiAliasing", "0"),
        ("bBottomInnerOutterCircleOffsetEnable", "0"),
        ("bImageBlur", "0"),
        ("bImageBlurPixelEditable", "1"),
        ("bImageMask", "0"),
        ("bInnerOutterCircleOffsetEnable", "0"),
        ("bLockXYSizeRatio", "1"),
        ("bottomExposureTimeStrategy", "0"),
        ("bottomInnerCircleOffset", "0"),
        ("bottomLayerCount", str(settings.bottom_layer_count)),
        ("bottomLayCount", str(settings.bottom_layer_count)),
        ("bottomLayerExposureTime", f"{settings.bottom_exposure_time:g}"),
        ("bottomLayExposureTime", f"{settings.bottom_exposure_time:g}"),
        ("bottomLayerLiftHeight", f"{settings.bottom_layer_lift_height:g}"),
        ("bottomLayerLiftSpeed", f"{settings.bottom_layer_lift_speed:g}"),
        ("bottomLightIntensityPWM", str(settings.bottom_light_intensity_pwm)),
        ("bottomLightOffTime", f"{settings.bottom_light_off_time:g}"),
        ("bottomOutterCircleOffset", "0"),
        ("bottomRestTimeAfterLift", f"{settings.bottom_rest_time_after_lift:g}"),
        ("bottomRestTimeAfterRetract", f"{settings.bottom_rest_time_after_retract:g}"),
        ("bottomRestTimeBeforeLift", f"{settings.bottom_rest_time_before_lift:g}"),
        ("circleParityDifference", "0"),
        ("currProfile", name),
        ("edgeCompensationPixel", "0"),
        ("edgeCompensationTimePercent", "100"),
        ("endGcode", '"M106 S0;\\nG1 Z{machine_height} F25;\\nM18;\\n"'),
        ("fillDensity", "30"),
        ("fillPattern", "None"),
        ("imageBlurPixel", "2"),
        ("imageMaskFile", ""),
        ("innerCircleOffset", "0"),
        (
            "layerGcode",
            '"M6054 {image};show Image\\n'
            "G0 Z{rise_pos} F{rise_speed};\\n"
            "G0 Z{fall_pos} F{fall_speed};\\n"
            "G4 P{light_delay};\\n"
            "M106 S{light_pwm};light on\\n"
            "G4 P{exposure_time};\\n"
            'M106 S0; light off\\n"',
        ),
        ("layerHeight", f"{settings.layer_height:g}"),
        ("lightOffTime", f"{settings.light_off_time:g}"),
        ("machineDepth", f"{printer.machine_depth:g}"),
        ("machineHeight", f"{printer.machine_height:g}"),
        ("machineType", printer.machine_type),
        ("machineWidth", f"{printer.machine_width:g}"),
        ("minGreyLevel", "0"),
        ("normalDropSpeed", f"{settings.normal_drop_speed:g}"),
        ("normalExposureTime", f"{settings.normal_exposure_time:g}"),
        ("normalLayerLiftHeight", f"{settings.normal_layer_lift_height:g}"),
        ("normalLayerLiftSpeed", f"{settings.normal_layer_lift_speed:g}"),
        ("normalLightIntensityPWM", str(settings.normal_light_intensity_pwm)),
        ("normalRestTimeAfterLift", f"{settings.rest_time_after_lift:g}"),
        ("normalRestTimeAfterRetract", f"{settings.rest_time_after_retract:g}"),
        ("normalRestTimeBeforeLift", f"{settings.rest_time_before_lift:g}"),
        ("outterCircleOffset", "0"),
        ("projectType", printer.project_type),
        ("resinDensity", f"{resin.density:g}"),
        ("resinPrice", "30"),
        ("resinUnit", "$/L"),
        ("resolutionX", str(printer.resolution_x)),
        ("resolutionY", str(printer.resolution_y)),
        ("shellThickness", "1.2"),
        (
            "startGcode",
            '"G21;\\nG90;\\nM106 S0;\\nG28 Z0;\\n"',
        ),
        ("transitionLayerCount", str(settings.transition_layer_count)),
        ("transitionLayerType", "0"),
        ("zSlowUpDistance", "0"),
    ]
    return [f"{k}:{v}" for k, v in pairs]


def write_cfg(
    path: Path | str,
    printer: Printer,
    resin: Resin,
    settings: PrintSettings,
    profile_name: str | None = None,
) -> Path:
    out = Path(path)
    lines = build_cfg_lines(printer, resin, settings, profile_name=profile_name)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def settings_clipboard_text(settings: PrintSettings) -> str:
    """Ultra Print-tab fields only (no lift/retract; Advanced left at defaults)."""
    lines = [
        f"Layer height: {settings.layer_height:.3f} mm",
        f"Layer exposure: {settings.normal_exposure_time:.2f} s",
        f"Bottom exposure: {settings.bottom_exposure_time:.2f} s",
        f"Bottom layers: {settings.bottom_layer_count}",
        f"Transition layers: {settings.transition_layer_count}",
        f"Rest before lift: {settings.rest_time_before_lift:.2f} s",
        f"Rest after lift: {settings.rest_time_after_lift:.2f} s",
        f"Rest after retract: {settings.rest_time_after_retract:.2f} s",
        "",
        "Note: Lift/retract unused on tilt Ultras — use Fast/Slow on the printer.",
        "Advanced (AA, shrinkage, tolerance, mask) left at Chitubox defaults.",
    ]
    return "\n".join(lines)
