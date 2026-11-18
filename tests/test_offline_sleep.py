"""重启同一数据库，核对夜间离线结算与重复加载。"""
import pytest

from maiweave.core.models import PlanInput
from offline_sleep_cases import CASES, restart_overnight
from test_plugin import plugin


@pytest.mark.parametrize("case,expected", CASES)
async def test_offline_night(plugin, case, expected):
    await restart_overnight(plugin, PlanInput, case, expected)
