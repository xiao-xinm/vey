"""Render the source-reviewed project diagram; optional Pillow, no browser or network.

python scripts/render_architecture.py --font /path/to/a/Chinese/font.ttf
Produces an editable SVG and a portable PNG with matching geometry.
"""

import argparse
import html
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1700, 1320
BG, INK, MUTED = "#f3f6fb", "#16243b", "#506078"
BLUE, GREEN, PURPLE = "#2863cd", "#19785a", "#7555ae"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, default=Path("C:/Windows/Fonts/msyh.ttc"))
    parser.add_argument("--output", type=Path, default=Path("docs/assets"))
    args = parser.parse_args()
    if not args.font.is_file():
        parser.error("Provide --font pointing to an installed Chinese font")
    args.output.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    fonts = {}
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="title desc">',
        '<title id="title">Vey 项目结构总览</title>',
        '<desc id="desc">企微通过网关连接核心，核心使用模型并通过独立执行器操作 Docker。后台单独管理配置和读取审计，PostgreSQL 保存运行与评测数据。</desc>',
        f'<rect width="100%" height="100%" fill="{BG}"/>',
    ]

    def font(size):
        if size not in fonts:
            fonts[size] = ImageFont.truetype(str(args.font), size)
        return fonts[size]

    def text(x, y, value, size=22, color=INK):
        assert x + draw.textlength(value, font=font(size)) <= WIDTH - 20, value
        draw.text((x, y), value, font=font(size), fill=color, anchor="lt")
        svg.append(
            f'<text x="{x}" y="{y + size}" font-family="Microsoft YaHei,Noto Sans CJK SC,sans-serif" font-size="{size}" fill="{color}">{html.escape(value)}</text>'
        )

    def edge(points, color=BLUE, dashed=False):
        for start, end in zip(points, points[1:], strict=False):
            if dashed:
                length = math.dist(start, end)
                for offset in range(0, math.ceil(length), 16):
                    a, b = offset / length, min(offset + 8, length) / length
                    draw.line(
                        [
                            (
                                start[0] + (end[0] - start[0]) * a,
                                start[1] + (end[1] - start[1]) * a,
                            ),
                            (
                                start[0] + (end[0] - start[0]) * b,
                                start[1] + (end[1] - start[1]) * b,
                            ),
                        ],
                        fill=color,
                        width=3,
                    )
            else:
                draw.line([start, end], fill=color, width=3)
        start, end = points[-2:]
        angle = math.atan2(end[1] - start[1], end[0] - start[0])
        triangle = [
            end,
            (end[0] - 13 * math.cos(angle - 0.45), end[1] - 13 * math.sin(angle - 0.45)),
            (end[0] - 13 * math.cos(angle + 0.45), end[1] - 13 * math.sin(angle + 0.45)),
        ]
        draw.polygon(triangle, fill=color)
        dash = ' stroke-dasharray="8 8"' if dashed else ""
        svg.append(
            f'<polyline points="{" ".join(f"{x},{y}" for x, y in points)}" fill="none" stroke="{color}" stroke-width="3"{dash}/>'
        )
        svg.append(
            f'<polygon points="{" ".join(f"{x},{y}" for x, y in triangle)}" fill="{color}"/>'
        )

    def box(x, y, width, height, title, subtitle, lines, accent=BLUE):
        draw.rounded_rectangle(
            (x, y, x + width, y + height), 16, fill="white", outline="#d6dfea", width=2
        )
        draw.rounded_rectangle((x, y, x + 7, y + height), 3, fill=accent)
        svg.append(
            f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="16" fill="white" stroke="#d6dfea" stroke-width="2"/>'
        )
        svg.append(f'<rect x="{x}" y="{y}" width="7" height="{height}" rx="3" fill="{accent}"/>')
        text(x + 24, y + 24, title, 26, accent)
        text(x + 24, y + 64, subtitle, 18, MUTED)
        cursor = y + 103
        for line in lines:
            current = ""
            for char in line:
                if draw.textlength(current + char, font=font(21)) > width - 48:
                    text(x + 24, cursor, current, 21)
                    cursor += 31
                    current = ""
                current += char
            text(x + 24, cursor, current, 21)
            cursor += 31
        assert cursor <= y + height + 3, (title, cursor)

    text(70, 40, "VEY / 项目结构总览", 43)
    text(70, 101, "先看权限，再看数据流  ·  V1 交付候选  ·  2026-10-08", 23, MUTED)

    edge([(370, 230), (470, 230)])
    edge([(900, 230), (1040, 230)])
    text(927, 201, "/admin", 19)
    edge([(650, 325), (650, 450)])
    text(672, 371, "/wecom/callback", 19)
    edge([(470, 560), (370, 560)])
    edge([(470, 690), (410, 690), (410, 280), (370, 280)])
    text(72, 345, "异步回传经企微 API", 18, MUTED)
    edge([(900, 565), (1040, 565)])
    text(925, 505, "Unix Socket", 17)
    text(924, 533, "运维令牌", 19)
    edge([(1235, 325), (1235, 450)], GREEN)
    text(1255, 364, "独立管理令牌", 19, GREEN)
    text(1255, 393, "仅配置接口", 19, GREEN)
    edge([(650, 750), (650, 920)])
    text(671, 861, "任务 / 审计", 19)
    edge([(1110, 750), (1110, 845), (860, 845), (860, 920)])
    text(929, 811, "确认 / 策略", 19)
    edge([(1415, 750), (1415, 920)], GREEN)
    text(1435, 820, "Docker", 19, GREEN)
    text(1435, 849, "socket", 19, GREEN)
    edge([(1470, 245), (1590, 245), (1590, 1190), (875, 1190), (875, 1165)], MUTED, True)
    text(1503, 644, "只读", 19, MUTED)
    text(1503, 674, "任务与", 19, MUTED)
    text(1503, 704, "评测", 19, MUTED)
    edge([(370, 1035), (470, 1035)], PURPLE)
    text(389, 1001, "归档", 19, PURPLE)
    edge([(215, 920), (215, 750)], PURPLE, True)
    text(234, 861, "离线案例调用模型", 18, PURPLE)

    box(70, 170, 300, 155, "你 / 使用入口", "企业微信 · 管理后台", ["聊天运维 / 浏览器管理"])
    box(
        470,
        170,
        430,
        155,
        "HTTPS 网关 / Caddy",
        "唯一公网业务入口 · 路径分流",
        ["企微回调 → 核心；/admin → 后台"],
    )
    box(
        1040,
        170,
        430,
        155,
        "管理后台 / dashboard",
        "独立登录 · 单管理员",
        ["任务、评测、复核、配置与备份报告"],
        GREEN,
    )
    box(
        70,
        450,
        300,
        300,
        "外部模型服务",
        "核心和评测器按需调用",
        [
            "DeepSeek：理解 / 规划",
            "Jev：可选意图分类",
            "当前默认：hybrid",
            "Jev 已接入，默认关闭",
            "模型没有直接执行权",
        ],
        PURPLE,
    )
    box(
        470,
        450,
        430,
        300,
        "Agent 核心 / agent-core",
        "app.py → Core → bounded diagnosis",
        [
            "身份校验、去重与任务队列",
            "规则优先，必要时调用模型",
            "最多 5 次只读工具 / 60 秒",
            "结构化证据、中文结果、Outbox",
            "不挂载 Docker socket",
        ],
    )
    box(
        1040,
        450,
        430,
        300,
        "受控执行器 / ops-executor",
        "固定工具 + 独立权限检查",
        [
            "只读查询与启停重启",
            "保护对象、会话、容器身份复核",
            "两分钟确认；未知结果不重放",
            "配置版本、预览、发布与回滚",
            "唯一持有 Docker socket 的组件",
        ],
        GREEN,
    )
    box(
        70,
        920,
        300,
        245,
        "离线评测工具",
        "evaluation/ · 独立运行",
        ["案例 → 模型 → 契约评分", "不执行真实运维工具", "结果追加归档", "保留失败和未知费用"],
        PURPLE,
    )
    box(
        470,
        920,
        430,
        245,
        "PostgreSQL / 两个数据库",
        "同一现有 PG 实例 · 角色隔离",
        [
            "vey_core：会话 / 任务 / 事件 / 投递",
            "vey_exec：确认 / 游标 / 配置版本",
            "vey_eval：实验与逐条样本",
            "后台读投影视图及评测只读账号",
        ],
    )
    box(
        1040,
        920,
        430,
        245,
        "Ubuntu / 受管理容器",
        "既有 Docker Compose 服务",
        [
            "仅操作登记对象，不提供任意 Shell",
            "PG / Redis / Agent 等对象受保护",
            "宿主机指标来自只读映射",
            "MinIO 是既有服务，不是 Agent 依赖",
        ],
        GREEN,
    )
    text(
        70,
        1238,
        "维护边界：管理员脚本执行本机备份与隔离恢复；后台只读报告。定时、加密与异地备份仍延期。",
        21,
        MUTED,
    )
    text(
        70,
        1276,
        "图中箭头表示调用或数据访问。执行器被攻破仍有高权限风险；权限分离不等于完整沙箱。",
        19,
        MUTED,
    )
    svg.append("</svg>")
    (args.output / "vey-architecture.svg").write_text("\n".join(svg), encoding="utf-8")
    image.save(args.output / "vey-architecture.png")
    print("Generated docs/assets/vey-architecture.svg and .png")


if __name__ == "__main__":
    main()
