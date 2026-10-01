"""Every quickbar field must be accepted by the method it calls.

The web quickbars send each declared param as a keyword argument, so a
param the method does not accept fails every click with a TypeError.
"""
import importlib
import inspect

import pytest

from AFL.automation.APIServer.Driver import Driver

DRIVERS = [
    "AFL.automation.loading.PneumaticPressureSampleCell.PneumaticPressureSampleCell",
    "AFL.automation.loading.PneumaticPressureSampleCellRevPi.PneumaticPressureSampleCellRevPi",
    "AFL.automation.prepare.OT2HTTPDriver.OT2HTTPDriver",
    "AFL.automation.prepare.FlexHTTPDriver.FlexHTTPDriver",
    "AFL.automation.prepare.VirtualOT2HTTPDriver.VirtualOT2HTTPDriver",
    "AFL.automation.prepare.VirtualFlexHTTPDriver.VirtualFlexHTTPDriver",
]


def load(path):
    module, name = path.rsplit(".", 1)
    try:
        return getattr(importlib.import_module(module), name)
    except ImportError as error:
        pytest.skip(f"cannot import {path}: {error}")


def quickbar_cases():
    for path in DRIVERS:
        yield pytest.param(path, id=path.rsplit(".", 1)[1])


@pytest.mark.parametrize("path", quickbar_cases())
def test_quickbar_params_are_accepted_by_method(path):
    cls = load(path)
    problems = []
    for name, info in Driver.quickbar.function_info.items():
        method = getattr(cls, name, None)
        if method is None or "qb" not in info:
            continue
        params = inspect.signature(method).parameters
        if any(p.kind is p.VAR_KEYWORD for p in params.values()):
            continue
        for field in info["qb"].get("params", {}):
            if field not in params:
                problems.append(f"{name}: quickbar field '{field}' not accepted")

    assert problems == []
