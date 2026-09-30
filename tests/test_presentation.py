from vey.core import render_result


def test_system_summary_converts_units_and_keeps_sample_time():
    result = {
        "available": True,
        "sampled_at": "2026-09-30T10:39:48.552699+00:00",
        "cpu_percent": 9.84,
        "loadavg": "0.08 0.12 0.05 3/457 35560",
        "uptime": "3027735.72 11954574.71",
        "memory": {
            "MemTotal": 3899994112,
            "MemAvailable": 2686410752,
            "SwapTotal": 0,
            "SwapFree": 0,
        },
        "disks": [{"total_bytes": 42156257280, "available_bytes": 27842727936}],
        "processes_by_memory": [{"name": "minio", "rss_kb": 142052}],
    }
    rendered = render_result(result, "system")
    for expected in (
        "2026-09-30 18:39:48",
        "CPU：9.8%",
        "35 天 1 小时 2 分钟",
        "1.13 GiB / 3.63 GiB（31.1%）",
        "可用 25.93 GiB / 总量 39.26 GiB",
        "minio：138.7 MiB",
        "Swap：未配置",
    ):
        assert expected in rendered
    assert "35560" not in rendered and "configured_host_proc" not in rendered


def test_missing_system_metrics_do_not_become_zero_or_healthy():
    rendered = render_result({"available": True, "memory": None, "cpu_percent": None}, "system")
    assert "CPU：暂不可用" in rendered and "内存：暂不可用" in rendered
    assert "Swap：暂不可用" in rendered and "采样时间：暂不可用" in rendered
    assert "0.0%" not in rendered and "正常" not in rendered
    assert "无法采集" in render_result({"available": False}, "system")
