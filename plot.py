"""
results/ のデータからグラフを描き、予想との対照表を出す

  uv run plot.py                           # results/ の run をすべて描いて figures/ に書き出す
  uv run plot.py sync async                # 指定した名前の run だけ
  uv run plot.py --results results/stable  # ぶれない設定の結果。figures/stable/ に書き出す
  docker compose run --rm plot             # Docker で（日本語フォント入り）

  status.png           結果の内訳（200 / 500 / 応答なし）を A〜D で並べたもの
  responses.png        応答が返ったタイミング（累積）を A〜D で並べたもの
  timeline-<name>.png  run ごとの時系列: クライアントが受け取った応答と、①②③ の観測値

--repeat で繰り返した run は、200 の件数が中央値の回で図を描き、対照表には中央値と幅を出す。
"""
import argparse
import csv
import json
import logging
import os
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import MaxNLocator

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
FIGURES = ROOT / "figures"

LABELS = {"baseline": "A /baseline", "blocking": "B /blocking", "sync": "C /sync", "async": "D /async"}
# 計画書 2.6 の予想（60 秒で 200 が返る件数）
PREDICTED_OK = {"baseline": "1000", "blocking": "約 12", "sync": "約 180", "async": "約 90"}

# run ごとに時系列で見る値（計画書 2.8）
PANELS = {
    "baseline": ["tasks"],
    "blocking": ["qlen"],
    "sync": ["tasks", "threads", "sync_checked_out"],
    "async": ["tasks", "async_checked_out"],
}
SERIES = {
    "qlen": "① accept queue の長さ",
    "tasks": "② ループ上のタスク数",
    "threads": "② スレッド数",
    "sync_checked_out": "③ プールから貸し出し中の接続（同期）",
    "async_checked_out": "③ プールから貸し出し中の接続（非同期）",
}

WHITE = "#ffffff"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SHADE = "#f0efec"
OK = "#2a78d6"      # 200
ERROR = "#eb6834"   # 500
OTHER = "#1baf7a"   # その他のステータス
NONE = "#c3c2b7"    # 応答なし
LINE = INK2         # サーバー側の観測値

JP_FONTS = ["Hiragino Sans", "Hiragino Kaku Gothic ProN", "Yu Gothic", "Noto Sans CJK JP", "IPAexGothic", "IPAGothic"]


def setup_style():
    installed = {f.name for f in font_manager.fontManager.ttflist}
    jp = [f for f in JP_FONTS if f in installed]
    if not jp:
        print("注意: 日本語フォントが見つからないので、ラベルが文字化けするかもしれない")
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
    plt.rcParams.update({
        "font.family": ["DejaVu Sans", *jp],  # 英数字は DejaVu、日本語はその次のフォントで描く
        "font.size": 9,
        "figure.facecolor": WHITE,
        "axes.facecolor": WHITE,
        "savefig.facecolor": WHITE,
        "axes.edgecolor": AXIS,
        "axes.linewidth": 0.8,
        "axes.labelcolor": INK2,
        "axes.titlecolor": INK,
        "axes.titlesize": 9,
        "axes.titlelocation": "left",
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "axes.grid.axis": "y",
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "xtick.color": AXIS,
        "ytick.color": AXIS,
        "xtick.labelcolor": INK2,
        "ytick.labelcolor": INK2,
        "legend.frameon": False,
        "lines.linewidth": 1.5,
        "lines.solid_capstyle": "round",
        "lines.solid_joinstyle": "round",
    })


def number(s):
    return float(s) if s != "" else None


def load_run(run_dir):
    meta = json.loads((run_dir / "meta.json").read_text())
    t0 = meta["hey_start"]
    with open(run_dir / "hey.csv", newline="") as f:
        # offset は hey がリクエストを送り始めた時刻。応答が返った時刻 = offset + response-time
        responses = sorted(
            (float(r["offset"]) + float(r["response-time"]), int(r["status-code"])) for r in csv.DictReader(f)
        )
    with open(run_dir / "probe.csv", newline="") as f:
        probe = [{k: number(v) for k, v in r.items()} for r in csv.DictReader(f)]
    for row in probe:
        row["t"] = row["timestamp"] - t0
    return {
        "name": run_dir.parent.name,
        "repeat": meta.get("repeat", 1),
        "endpoint": meta["endpoint"],
        "meta": meta,
        "responses": responses,
        "probe": probe,
        "end": probe[-1]["t"],
    }


def outcome(run):
    codes = [code for _, code in run["responses"]]
    ok = codes.count(200)
    error = codes.count(500)
    return {
        "200": ok,
        "500": error,
        "その他": len(codes) - ok - error,
        "応答なし": run["meta"]["requests"] - len(codes),
    }


def load_groups(results_dir, names):
    """results/<name>/<何回目>/ を name ごとにまとめて読む。hey が終わる前に止めた回は飛ばす"""
    groups = []
    for group_dir in sorted(p for p in results_dir.iterdir() if p.is_dir()):
        if names and group_dir.name not in names:
            continue
        runs = []
        for run_dir in sorted((d for d in group_dir.iterdir() if d.name.isdigit()), key=lambda d: int(d.name)):
            meta_path = run_dir / "meta.json"
            if not meta_path.exists():
                continue
            if "hey_end" not in json.loads(meta_path.read_text()):
                print(f"スキップ: {group_dir.name}/{run_dir.name}（hey が終わる前に止めた回）")
                continue
            runs.append(load_run(run_dir))
        if runs:
            groups.append(runs)
    return groups


def representative(runs):
    """200 の件数が中央値の回（偶数回なら少ない方）。図はこの回で描き、runs に全部の回を持たせる"""
    ranked = sorted(runs, key=lambda r: (outcome(r)["200"], r["repeat"]))
    return {**ranked[(len(ranked) - 1) // 2], "runs": runs}


def label_of(run):
    label = LABELS[run["endpoint"]]
    return label if run["name"] == run["endpoint"] else f"{label}（{run['name']}）"


def cumulative(run, code, until):
    """(時刻, 累積件数) の階段。0 から until まで"""
    times = [t for t, c in run["responses"] if c == code]
    xs, ys = [0.0], [0]
    for i, t in enumerate(times, 1):
        xs += [t, t]
        ys += [i - 1, i]
    xs.append(until)
    ys.append(len(times))
    return xs, ys


def text_color(fill):
    """塗りの上に置く文字は、塗りの明るさで白か墨かを選ぶ"""
    r, g, b = (int(fill[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lum = sum(w * (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4) for w, c in zip((0.2126, 0.7152, 0.0722), (r, g, b)))
    return WHITE if 1.05 / (lum + 0.05) >= 4.5 else INK


def plot_status(runs, path):
    fig, ax = plt.subplots(figsize=(7, 0.5 * len(runs) + 1.1))
    colors = {"200": OK, "500": ERROR, "その他": OTHER, "応答なし": NONE}
    n = max(r["meta"]["requests"] for r in runs)
    segments = []
    for i, run in enumerate(runs):
        left = 0
        for key, value in outcome(run).items():
            if value:
                ax.barh(i, value, left=left, height=0.5, color=colors[key], edgecolor=WHITE, linewidth=1.5, label=key)
                segments.append((i, key, left, value))
                left += value
    ax.set_yticks(range(len(runs)), [label_of(r) for r in runs])
    ax.invert_yaxis()
    ax.set_xlim(0, n * 1.18)
    ax.set_xticks([t for t in ax.get_xticks() if t <= n])
    ax.set_xlabel("リクエスト数")
    ax.tick_params(axis="y", length=0)
    ax.grid(False)
    ax.spines["left"].set_visible(False)
    present = [key for key in colors if any(key == k for _, k, _, _ in segments)]
    handles = [Patch(color=colors[key], label=key) for key in present]
    ax.legend(handles=handles, loc="lower left", bbox_to_anchor=(0, 1.02), ncol=len(handles))
    title = f"{n} 並列で叩いた結果の内訳（クライアント側のタイムアウト {runs[0]['meta']['client_timeout']} 秒）"
    if any(len(r["runs"]) > 1 for r in runs):
        title += "\n繰り返した条件は、200 の件数が中央値の回"
    ax.set_title(title, pad=28)
    fig.tight_layout()

    # レイアウトが決まってから、数字が棒の中に入るかを測る。入らないものは棒の右に回す
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    outside = {}
    for i, key, left, value in segments:
        label = ax.text(left + value / 2, i, f"{value}", ha="center", va="center", color=text_color(colors[key]))
        x0, x1 = ax.transData.transform([(left, i), (left + value, i)])[:, 0]
        if label.get_window_extent(renderer).width + 8 > x1 - x0:
            label.remove()
            outside.setdefault(i, []).append(f"{key}: {value}")
    for i, texts in outside.items():
        ax.text(n * 1.015, i, "  ".join(texts), va="center", color=INK2)
    fig.savefig(path, dpi=200)
    plt.close(fig)


def draw_responses(ax, run, until):
    """クライアント（hey）が受け取った応答の累積。行末に件数を添える"""
    for code, color in ((200, OK), (500, ERROR)):
        xs, ys = cumulative(run, code, until)
        if ys[-1] or code == 200:
            ax.plot(xs, ys, color=color, label=str(code))
            ax.annotate(f"{code}: {ys[-1]}", (until, ys[-1]), xytext=(4, 0), textcoords="offset points",
                        va="center", color=INK2, annotation_clip=False)
    ax.set_ylim(0, run["meta"]["requests"] * 1.05)
    ax.yaxis.set_major_locator(MaxNLocator(4, integer=True))


def mark_time(ax, t, text=None):
    """縦の参照線（実線のヘアライン）。text はパネルのタイトルより上の段に添える"""
    ax.axvline(t, color=MUTED, linewidth=0.8, zorder=0)
    if text:
        ax.annotate(text, (t, 1), xycoords=("data", "axes fraction"), xytext=(3, 17), textcoords="offset points",
                    color=MUTED, fontsize=8, va="bottom")


def reference_times(run):
    """hey がタイムアウトまで待った run にはその時刻、/async には pool_timeout（全員が 0 秒から待ち始めるので線が意味を持つ）"""
    meta = run["meta"]
    marks = []
    if meta["hey_end"] - meta["hey_start"] >= meta["client_timeout"] - 1:
        marks.append((meta["client_timeout"], f"hey のタイムアウト（{meta['client_timeout']} 秒）"))
    if run["endpoint"] == "async":
        marks.append((meta["pool"]["pool_timeout"], f"pool_timeout（{meta['pool']['pool_timeout']} 秒）"))
    return marks


def plot_responses(runs, path):
    until = max(r["meta"]["client_timeout"] for r in runs) + 3
    fig, axes = plt.subplots(len(runs), 1, sharex=True, figsize=(7, 1.4 * len(runs) + 0.8), squeeze=False)
    labeled = set()  # 同じ参照線のラベルは最初に出てくるパネルにだけ添える
    for ax, run in zip(axes[:, 0], runs):
        draw_responses(ax, run, until)
        ax.set_title(label_of(run))
        for t, text in reference_times(run):
            mark_time(ax, t, None if text in labeled else text)
            labeled.add(text)
    axes[-1, 0].set_xlim(0, until)
    axes[-1, 0].set_xlabel("負荷をかけ始めてからの秒数")
    fig.suptitle("クライアントが受け取った応答（累積件数）", x=0.01, y=0.99, ha="left", fontsize=10, color=INK)
    handles = [Line2D([], [], color=OK, label="200"), Line2D([], [], color=ERROR, label="500")]
    fig.legend(handles=handles, loc="upper right", bbox_to_anchor=(0.99, 1.0), ncol=2)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(path, dpi=200)
    plt.close(fig)


def silent_spans(probe):
    """/stats が返らなかった区間 [(開始, 終了)]"""
    spans, start = [], None
    for prev, row in zip(probe, probe[1:] + [None]):
        if prev["tasks"] is None and start is None:
            start = prev["t"]
        if start is not None and (row is None or row["tasks"] is not None):
            spans.append((start, row["t"] if row else prev["t"]))
            start = None
    return spans


def limit_of(run, key):
    """その値の上限。① は netstat / ss が出す上限、③ は pool_size + max_overflow"""
    if key == "qlen":
        return max((row["qmax"] for row in run["probe"] if row["qmax"] is not None), default=None)
    if key.endswith("checked_out"):
        return run["meta"]["pool"]["pool_size"] + run["meta"]["pool"]["max_overflow"]
    return None


def plot_timeline(run, path):
    panels = PANELS[run["endpoint"]]
    meta = run["meta"]
    start, end = run["probe"][0]["t"], max(run["end"], meta["hey_end"] - meta["hey_start"])
    fig, axes = plt.subplots(len(panels) + 1, 1, sharex=True, figsize=(7, 1.35 * (len(panels) + 1) + 0.8))
    draw_responses(axes[0], run, end)
    axes[0].set_title("クライアント（hey）が受け取った応答（累積件数）")
    axes[0].legend(loc="upper left", bbox_to_anchor=(0, 1.0), ncol=2)
    spans = silent_spans(run["probe"])
    ts = [row["t"] for row in run["probe"]]
    for ax, key in zip(axes[1:], panels):
        values = [row[key] for row in run["probe"]]
        limit = limit_of(run, key)
        ax.plot(ts, [float("nan") if v is None else v for v in values], color=LINE)
        ax.set_title(SERIES[key])
        ax.set_ylim(0, max(v for v in values + [limit, 1] if v is not None) * 1.15)
        if limit:
            ax.axhline(limit, color=MUTED, linewidth=0.8, zorder=0)
            ax.annotate(f"上限 {limit:.0f}", (1, limit), xycoords=("axes fraction", "data"),
                        xytext=(4, 0), textcoords="offset points", va="center", color=MUTED, fontsize=8)
        ax.yaxis.set_major_locator(MaxNLocator(4, integer=True))
        for a, b in spans:
            ax.axvspan(a, b, color=SHADE, linewidth=0, zorder=0)
    if spans:
        a, _ = max(spans, key=lambda s: s[1] - s[0])
        axes[1].annotate("/stats が返らない", (a, 0.5), xycoords=("data", "axes fraction"), xytext=(4, 0),
                         textcoords="offset points", va="center", color=MUTED, fontsize=8)
    for i, ax in enumerate(axes):
        for t, text in reference_times(run):
            if start <= t <= end:
                mark_time(ax, t, text if i == 0 else None)
    axes[-1].set_xlim(start, end)
    axes[-1].set_xlabel("負荷をかけ始めてからの秒数")
    title = label_of(run)
    if len(run["runs"]) > 1:
        title += f"　{len(run['runs'])} 回のうち 200 が中央値の {run['repeat']} 回目"
    fig.suptitle(title, x=0.01, ha="left", fontsize=11, color=INK, fontweight="bold")
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)


def peak(run, key):
    """負荷をかけ始めてからの最大値。/stats が一度も返らなければ None"""
    return max((row[key] for row in run["probe"] if row["t"] >= 0 and row[key] is not None), default=None)


def print_table(runs):
    def fmt(v):
        return "-" if v is None else f"{v:.0f}"

    def spread(run, key):
        """1 回なら件数、繰り返したなら 中央値（最小〜最大）"""
        values = [outcome(r)[key] for r in run["runs"]]
        median = f"{statistics.median(values):g}"
        return median if min(values) == max(values) else f"{median}（{min(values)}〜{max(values)}）"

    print("| エンドポイント | 回数 | 予想（200） | 200 | 500 | 応答なし | threads | tasks | checked out | accept queue | pg_sleep |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for run in runs:
        predicted = PREDICTED_OK[run["endpoint"]] if run["name"] == run["endpoint"] else "-"
        pools = [v for v in (peak(run, "sync_checked_out"), peak(run, "async_checked_out")) if v is not None]
        print(
            f"| {label_of(run)} | {len(run['runs'])} | {predicted} "
            f"| {spread(run, '200')} | {spread(run, '500')} | {spread(run, '応答なし')} "
            f"| {fmt(peak(run, 'threads'))} | {fmt(peak(run, 'tasks'))} | {fmt(max(pools, default=None))} "
            f"| {fmt(peak(run, 'qlen'))} | {fmt(peak(run, 'pg_active'))} |"
        )
    print("\n200・500・応答なしは、繰り返した run では中央値（最小〜最大）。")
    print("threads 以降は、200 が中央値の回の、負荷をかけ始めてからの最大値。- は /stats が返らず測れなかった。")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="*", help="results/ の下の run 名（省略するとすべて）")
    parser.add_argument("--results", default=os.environ.get("RESULTS_DIR", "results"),
                        help="読む結果のディレクトリ（既定は環境変数 RESULTS_DIR か results）")
    args = parser.parse_args()

    results_dir = (ROOT / args.results).resolve()
    try:
        out_dir = FIGURES / results_dir.relative_to(RESULTS)  # results/stable → figures/stable
    except ValueError:
        out_dir = FIGURES / results_dir.name
    if not results_dir.is_dir():
        raise SystemExit(f"{args.results} がない。先に bench.py <endpoint> を実行する")
    order = list(LABELS)
    runs = sorted(
        (representative(g) for g in load_groups(results_dir, args.names)),
        key=lambda r: (order.index(r["endpoint"]), r["name"] != r["endpoint"], r["name"]),
    )
    if not runs:
        raise SystemExit(f"{args.results} にデータがない。先に bench.py <endpoint> を実行する")

    setup_style()
    out_dir.mkdir(parents=True, exist_ok=True)
    main_runs = [r for r in runs if r["name"] == r["endpoint"]]
    if main_runs:
        plot_status(main_runs, out_dir / "status.png")
        plot_responses(main_runs, out_dir / "responses.png")
    for run in runs:
        plot_timeline(run, out_dir / f"timeline-{run['name']}.png")
    print_table(runs)
    print(f"\nグラフ: {out_dir.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
