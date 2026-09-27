#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GEX Bot — cập nhật Gamma Exposure (GEX) levels trước giờ mở cửa phiên Mỹ.

Nguồn data : CBOE delayed quotes (miễn phí, không cần API key)
Tính       : Gamma Flip, Call Wall, Put Wall, Net GEX, top strikes theo GEX
Gửi tới    : Discord webhook
Lịch       : GitHub Actions cron — script tự kiểm tra giờ ET (tự động DST)

Cách chạy:
    python gex_bot.py --dry-run                          # xem thử, không gửi
    python gex_bot.py --force                            # gửi ngay bất kể giờ
    DISCORD_WEBHOOK_URL=... python gex_bot.py            # gửi theo khung giờ

Công thức GEX mỗi option (USD trên mỗi 1% di chuyển của spot):
    GEX = gamma x OI x 100 x spot^2 x 0.01 x (+1 call / -1 put)
Quy ước "naive dealer": dealer long call / short put (calls dương, puts âm).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

# ------------------------------- CẤU HÌNH -------------------------------
SYMBOLS = ["SPX", "QQQ", "SPY"]  # underlying cần tính GEX (SPX→ES/SPY)
PLAN_INSTRUMENTS = {             # sinh trade plan cho symbol nào, theo instrument gì
    "SPX": "SPY / ES futures",
    "QQQ": "QQQ",
    "SPY": "SPY / ES futures",
}
INDEX_SYMBOLS = {"NDX": "_NDX", "SPX": "_SPX", "VIX": "_VIX"}  # CBOE đặt dấu _ cho index
MINUTES_BEFORE_OPEN = 5         # mục tiêu: gửi trước giờ mở cửa 9:30 ET
TOLERANCE_BEFORE_MIN = 15       # dung sai chạy sớm (GitHub cron có thể lệch)
TOLERANCE_AFTER_OPEN_MIN = 30   # dung sai chạy muộn sau giờ mở cửa
STRIKE_WINDOW_PCT = 5.0         # chart chỉ gồm strike trong ±% so với spot
CHART_ROWS = 13                 # số dòng chart
NEAR_EXPIRY_DAYS = 9            # walls/chart chỉ tính option hết hạn trong N ngày
BOT_NAME = ""                   # tên bot trong Discord; "" = dùng tên & avatar của webhook
CONTRACT_SIZE = 100             # hệ số hợp đồng option Mỹ
# -------------------------------------------------------------------------

CBOE_URL = "https://cdn-api.cboe.com/api/global/delayed_quotes/options/{sym}.json"
OCC_RE = re.compile(r"^(?P<root>[A-Z]+)(?P<exp>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")
ET = ZoneInfo("America/New_York")
VN = ZoneInfo("Asia/Ho_Chi_Minh")

# NYSE holidays 2026-2027 (cập nhật hàng năm)
NYSE_HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}


def now_et() -> datetime:
    return datetime.now(ET)


def next_session_date(d: date) -> date:
    """Ngày giao dịch kế tiếp (bỏ cuối tuần + holiday)."""
    while d.weekday() >= 5 or d in NYSE_HOLIDAYS:
        d += timedelta(days=1)
    return d


def send_window(now: datetime) -> tuple[bool, str]:
    """True nếu đang trong khung 'trước phiên' theo giờ ET (tự DST)."""
    if now.weekday() >= 5:
        return False, "cuối tuần — thị trường đóng cửa"
    if now.date() in NYSE_HOLIDAYS:
        return False, "NYSE holiday — thị trường đóng cửa"
    open_ = now.replace(hour=9, minute=30, second=0, microsecond=0)
    lo = open_ - timedelta(minutes=MINUTES_BEFORE_OPEN + TOLERANCE_BEFORE_MIN)
    hi = open_ + timedelta(minutes=TOLERANCE_AFTER_OPEN_MIN)
    if lo <= now < hi:
        mins = (open_ - now).total_seconds() / 60.0
        return True, f"hợp lệ — trước giờ mở cửa {mins:.0f} phút"
    return False, f"ngoài khung giờ gửi {lo:%H:%M}–{hi:%H:%M} ET (hiện tại {now:%H:%M} ET)"


# --------------------------------- DATA ----------------------------------

def fetch_chain(symbol: str) -> dict:
    """Lấy toàn bộ options chain (delayed) từ CBOE cho 1 underlying."""
    sym = symbol.upper()
    url = CBOE_URL.format(sym=INDEX_SYMBOLS.get(sym, sym))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (gex-bot/1.0)"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = payload.get("data", {})
    spot = data.get("current_price") or data.get("close")
    if not spot:
        raise ValueError("không đọc được giá spot từ payload CBOE")

    rows, skipped = [], 0
    for o in data.get("options", []):
        m = OCC_RE.match(o.get("option", ""))
        if not m:
            skipped += 1
            continue
        gamma = o.get("gamma") or 0.0
        delta = o.get("delta") or 0.0
        oi = o.get("open_interest") or 0.0
        if oi <= 0 or (gamma <= 0 and abs(delta) <= 0):
            continue
        rows.append({
            "strike": int(m.group("strike")) / 1000.0,
            "cp": m.group("cp"),
            "gamma": float(gamma),
            "delta": float(delta),
            "oi": float(oi),
            "exp": datetime.strptime(m.group("exp"), "%y%m%d").date(),
        })
    if not rows:
        raise ValueError("chain rỗng hoặc không parse được option nào")
    return {
        "symbol": sym,
        "spot": float(spot),
        "iv30": data.get("iv30"),
        "rows": rows,
        "timestamp": payload.get("timestamp", ""),
        "skipped": skipped,
    }


def fetch_vol_index(symbol: str = "VIX") -> dict:
    """Lấy VIX spot + net GEX trên VIX options chain (CBOE delayed)."""
    url = CBOE_URL.format(sym=INDEX_SYMBOLS.get(symbol.upper(), symbol.upper()))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (gex-bot/1.0)"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = payload.get("data", {})
    spot = data.get("current_price") or data.get("close")
    if not spot:
        raise ValueError("không đọc được VIX spot")
    spot = float(spot)
    net_gex = 0.0
    for o in data.get("options", []):
        m = OCC_RE.match(o.get("option", ""))
        if not m:
            continue
        gamma = o.get("gamma") or 0.0
        oi = o.get("open_interest") or 0.0
        if gamma <= 0 or oi <= 0:
            continue
        net_gex += (gamma * oi * CONTRACT_SIZE * spot * spot * 0.01
                    * (1.0 if m.group("cp") == "C" else -1.0))
    return {"symbol": symbol.upper(), "spot": spot, "net_gex": net_gex,
            "timestamp": payload.get("timestamp", "")}



# -------------------------------- TÍNH TOÁN -------------------------------

def compute_levels(chain: dict, session_date: date, max_expiry_days: int | None = None) -> dict:
    """Tính GEX theo strike và các mức quan trọng."""
    spot, rows = chain["spot"], chain["rows"]
    if max_expiry_days is not None:
        rows = [r for r in rows if 0 <= (r["exp"] - session_date).days <= max_expiry_days]

    scale = CONTRACT_SIZE * spot * spot * 0.01          # $ GEX trên 1% move
    net_by_strike: dict[float, float] = {}
    by_exp: dict[date, dict[float, float]] = {}
    total = 0.0
    total_dex = 0.0
    front_dex = 0.0
    front_exp = min((r["exp"] for r in rows if r["exp"] >= session_date), default=None)
    for r in rows:
        if r["gamma"] > 0:
            gex = r["gamma"] * r["oi"] * scale * (1.0 if r["cp"] == "C" else -1.0)
            net_by_strike[r["strike"]] = net_by_strike.get(r["strike"], 0.0) + gex
            d = by_exp.setdefault(r["exp"], {})
            d[r["strike"]] = d.get(r["strike"], 0.0) + gex
            total += gex
        if r.get("delta"):
            dex = r["delta"] * r["oi"] * CONTRACT_SIZE * spot   # $ Delta Exposure
            total_dex += dex
            if r["exp"] == front_exp:
                front_dex += dex

    if not net_by_strike:
        raise ValueError("không có option nào có gamma > 0")
    call_wall = max(net_by_strike, key=net_by_strike.get)
    put_wall = min(net_by_strike, key=net_by_strike.get)
    flip = find_gamma_flip(net_by_strike)

    front = None
    exps = sorted(e for e in by_exp if e >= session_date)
    if exps:
        fe = exps[0]
        fnet = by_exp[fe]
        front = {
            "date": fe,
            "dte": (fe - session_date).days,
            "net": sum(fnet.values()),
            "call_wall": max(fnet, key=fnet.get),
            "put_wall": min(fnet, key=fnet.get),
        }
    return {
        "spot": spot,
        "net_by_strike": net_by_strike,
        "call_wall": call_wall,
        "call_wall_gex": net_by_strike[call_wall],
        "put_wall": put_wall,
        "put_wall_gex": net_by_strike[put_wall],
        "gamma_flip": flip,
        "total_gex": total,
        "total_dex": total_dex,
        "front_dex": front_dex,
        "front": front,
        "max_days": max_expiry_days,
    }


def find_gamma_flip(net_by_strike: dict[float, float]) -> float | None:
    """Strike (nội suy tuyến tính) nơi tổng GEX lũy kế đổi dấu từ âm sang dương."""
    cum, prev_k, prev_cum = 0.0, None, None
    for k in sorted(net_by_strike):
        prev_cum, prev_k = cum, k if prev_k is None else prev_k
        cum += net_by_strike[k]
        if prev_cum is not None and prev_cum < 0.0 <= cum:
            if prev_k is None or cum == prev_cum:
                return k
            w = max(0.0, min(1.0, -prev_cum / (cum - prev_cum)))
            return prev_k + (k - prev_k) * w
        prev_k = k
    return None


# -------------------------------- TRÌNH BÀY -------------------------------

def fmt_gex(v: float, width: int | None = None) -> str:
    a = abs(v)
    if a >= 1e9:
        s = f"${a / 1e9:.2f}B"
    elif a >= 1e6:
        s = f"${a / 1e6:.1f}M"
    elif a >= 1e3:
        s = f"${a / 1e3:.0f}K"
    else:
        s = f"${a:.0f}"
    out = ("+" if v >= 0 else "-") + s
    return f"{out:>{width}}" if width else out


def fmt_strike(k: float) -> str:
    return f"{k:,.0f}" if k >= 1000 else f"{k:g}"


def build_chart(net_by_strike: dict[float, float], spot: float) -> str:
    """Chart bar ASCII: net GEX theo strike quanh spot."""
    ks = [k for k in net_by_strike if abs(k / spot - 1.0) * 100.0 <= STRIKE_WINDOW_PCT]
    top = sorted(ks, key=lambda k: abs(net_by_strike[k]), reverse=True)[:CHART_ROWS]
    top.sort()
    if not top:
        return f"(không có strike trong ±{STRIKE_WINDOW_PCT:.1f}% quanh spot)"
    mx = max(abs(net_by_strike[k]) for k in top) or 1.0
    nearest = min(top, key=lambda k: abs(k - spot))
    lines = []
    for k in top:
        v = net_by_strike[k]
        bar = "█" * max(1, round(abs(v) / mx * 16))
        mark = "  ◄ gần spot" if k == nearest else ""
        lines.append(f"{fmt_strike(k):>8} │{fmt_gex(v, 9)} {bar}{mark}")
    return "\n".join(lines)


def build_report(chain: dict, lv: dict, session_date: date,
                 image_file: str | None = None) -> tuple[dict, str]:
    """Trả về (Discord embed, plain-text/markdown) cho 1 underlying.

    image_file: tên file PNG đính kèm — nếu có, embed hiện ảnh chart
    (attachment://) thay vì chart ASCII.
    """
    sym, spot = chain["symbol"], lv["spot"]
    flip, total, front = lv["gamma_flip"], lv["total_gex"], lv["front"]

    if total >= 0:
        regime = "gamma DƯƠNG — hedging của dealer thường chặn biến động (giá hay bị 'pin')"
    else:
        regime = "gamma ÂM — hedging của dealer thường thổi bùng biến động"

    if flip is None:
        flip_line = "— (không có điểm đổi dấu)"
    else:
        pct = (spot / flip - 1.0) * 100.0
        side = "TRÊN" if pct >= 0 else "DƯỚI"
        flip_line = f"{flip:,.2f} — spot {side} flip {abs(pct):.1f}%"

    chart = build_chart(lv["net_by_strike"], spot)

    front_line = ""
    if front:
        tag = "0DTE" if front["dte"] == 0 else f"{front['dte']}DTE"
        front_line = (
            f"Front expiry {front['date']:%d/%m} ({tag}): net {fmt_gex(front['net'])}"
            f" · CW {fmt_strike(front['call_wall'])} · PW {fmt_strike(front['put_wall'])}"
        )

    nd = lv.get("near_days")
    nd_tag = f" (≤{nd} ngày)" if nd else ""

    # ---- plain text (console / file) ----
    lines = [
        f"## {sym} · Gamma Levels — trước phiên {session_date:%a %d/%m/%Y}",
        f"- Spot (close): **{spot:.2f}**",
        f"- Gamma Flip: **{flip_line}**",
        f"- Call Wall{nd_tag}: **{fmt_strike(lv['call_wall'])}** ({fmt_gex(lv['call_wall_gex'])})",
        f"- Put Wall{nd_tag}: **{fmt_strike(lv['put_wall'])}** ({fmt_gex(lv['put_wall_gex'])})",
        f"- Net GEX toàn expiry: **{fmt_gex(total)} / 1%** → {regime}",
    ]
    if front_line:
        lines.append(f"- {front_line}")
    lines += ["", "Net GEX theo strike ($ / 1% move):", "", chart, ""]
    if chain["timestamp"]:
        lines.append(f"_Data: CBOE delayed · cập nhật {chain['timestamp']}_")
    lines.append("_Tham khảo educational — không phải khuyến nghị đầu tư._")
    text = "\n".join(lines)

    # ---- Discord embed ----
    fields = [
        {"name": "Spot (close)", "value": f"{spot:.2f}", "inline": True},
        {"name": "Gamma Flip", "value": flip_line, "inline": True},
        {"name": "Net GEX (/1%)", "value": fmt_gex(total), "inline": True},
        {"name": "Call Wall",
         "value": f"{fmt_strike(lv['call_wall'])} · {fmt_gex(lv['call_wall_gex'])}",
         "inline": True},
        {"name": "Put Wall",
         "value": f"{fmt_strike(lv['put_wall'])} · {fmt_gex(lv['put_wall_gex'])}",
         "inline": True},
    ]
    if front:
        tag = "0DTE" if front["dte"] == 0 else f"{front['dte']}DTE"
        fields.append({
            "name": f"Front expiry · {front['date']:%d/%m} ({tag})",
            "value": f"net {fmt_gex(front['net'])} · CW {fmt_strike(front['call_wall'])}"
                     f" · PW {fmt_strike(front['put_wall'])}",
            "inline": False,
        })
    embed = {
        "title": f"{sym} · Gamma Levels — trước phiên {session_date:%a %d/%m/%Y}",
        "description": (f"{regime}" if image_file else
                        f"**Net GEX theo strike** ($ / 1% move):\n```{chart}```{regime}"),
        "color": 0x2ECC71 if total >= 0 else 0xE74C3C,
        "fields": fields,
        "footer": {"text": "Data: CBOE delayed · educational — không phải khuyến nghị đầu tư"},
        "timestamp": datetime.now(ET).isoformat(),
    }
    if image_file:
        embed["image"] = {"url": f"attachment://{image_file}"}
    return embed, text


# --------------------------------- GỬI ------------------------------------

def _multipart(fields: dict[str, str], files: list[tuple[str, str, bytes]],
               boundary: str) -> bytes:
    """Tạo body multipart/form-data (stdio-only, không cần requests)."""
    out = []
    for name, value in fields.items():
        out.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n"
            f"\r\n{value}\r\n".encode("utf-8"))
    for name, filename, blob in files:
        out.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n".encode("utf-8")
            + blob + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(out)


def send_discord(webhook: str, embeds: list[dict],
                 files: list[tuple[str, bytes]] | None = None) -> None:
    """POST embeds (+ ảnh đính kèm) lên Discord webhook.

    files: danh sách (tên_file.png, bytes). Ảnh gán vào embed qua
    attachment://<tên_file> (bot đã set embed["image"] tương ứng).
    """
    files = files or []
    for i in range(0, len(embeds), 10):
        chunk = embeds[i:i + 10]
        msg = {"embeds": chunk}
        if BOT_NAME:
            msg["username"] = BOT_NAME
        attach = files if i == 0 else []
        if attach:
            boundary = "----gexbot" + uuid4().hex
            payload = json.dumps(msg)
            body = _multipart(
                {"payload_json": payload},
                [(f"files[{j}]", fn, blob) for j, (fn, blob) in enumerate(attach)],
                boundary)
            ctype = f"multipart/form-data; boundary={boundary}"
        else:
            body = json.dumps(msg).encode("utf-8")
            ctype = "application/json"
        for attempt in range(3):
            req = urllib.request.Request(
                webhook, data=body,
                headers={"Content-Type": ctype, "User-Agent": "gex-bot/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    n = len(chunk)
                    extra = f" + {len(attach)} ảnh" if attach else ""
                    print(f"[ok] Đã gửi {n} embed{extra} tới Discord (HTTP {resp.status}).")
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    time.sleep(float(e.headers.get("Retry-After", "2")) + 1.0)
                    continue
                raise


# --------------------------------- MAIN -----------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Bot GEX levels hàng ngày trước phiên Mỹ (data CBOE miễn phí, gửi Discord).")
    p.add_argument("--symbols", nargs="+", default=SYMBOLS,
                   help=f"underlying cần tính, cách nhau bằng space (mặc định: {' '.join(SYMBOLS)})")
    p.add_argument("--force", action="store_true",
                   help="bỏ qua kiểm tra giờ ET, chạy ngay lập tức")
    p.add_argument("--dry-run", action="store_true",
                   help="chỉ in báo cáo ra console, không gửi Discord (luôn chạy bất kể giờ)")
    p.add_argument("--webhook", default=os.environ.get("DISCORD_WEBHOOK_URL", ""),
                   help="Discord webhook URL (hoặc đặt biến môi trường DISCORD_WEBHOOK_URL)")
    p.add_argument("--max-expiry-days", type=int, default=None,
                   help="chỉ tính option expiring trong N ngày tới (mặc định: mọi expiry)")
    p.add_argument("--no-image", action="store_true",
                   help="không vẽ chart ảnh, chỉ gửi embed text (không cần matplotlib)")
    p.add_argument("--out", default="", help="ghi báo cáo markdown ra file này")
    args = p.parse_args(argv)

    now = now_et()
    session_date = next_session_date(now.date())
    print(f"[i] Bây giờ: {now:%Y-%m-%d %H:%M:%S} ET · {datetime.now(VN):%H:%M} giờ VN"
          f" · phiên tới: {session_date:%a %d/%m/%Y}")

    if not (args.force or args.dry_run):
        ok, msg = send_window(now)
        print(f"[i] Kiểm tra khung giờ gửi: {msg}")
        if not ok:
            print("[skip] Không gửi. Dùng --force để chạy thử hoặc --dry-run để xem mẫu.")
            return 0

    vol_index = None
    try:
        vol_index = fetch_vol_index("VIX")
        print(f"[ok] VIX {vol_index['spot']:.2f} · net GEX VIX options "
              f"{fmt_gex(vol_index['net_gex'])}/1%")
    except Exception as e:
        print(f"[!] Không lấy được VIX ({e}) — dự phòng dùng IV30", file=sys.stderr)

    embeds, texts, files = [], [], []
    for sym in args.symbols:
        sym = sym.upper()
        try:
            print(f"[..] Lấy options chain {sym} từ CBOE...")
            chain = fetch_chain(sym)
            print(f"[ok] {sym}: {len(chain['rows']):,} options có OI, "
                  f"spot {chain['spot']:.2f}, data cập nhật {chain['timestamp']}")
            lv = compute_levels(chain, session_date, args.max_expiry_days)

            # walls/chart lấy từ cửa sổ gần hạn; flip & net GEX giữ toàn expiry
            near_days = (min(args.max_expiry_days, NEAR_EXPIRY_DAYS)
                         if args.max_expiry_days else NEAR_EXPIRY_DAYS)
            lv_near = compute_levels(chain, session_date, near_days)
            if lv_near["net_by_strike"]:
                lv.update({
                    "call_wall": lv_near["call_wall"],
                    "call_wall_gex": lv_near["call_wall_gex"],
                    "put_wall": lv_near["put_wall"],
                    "put_wall_gex": lv_near["put_wall_gex"],
                    "net_by_strike": lv_near["net_by_strike"],
                    "front": lv_near["front"],
                    "near_days": near_days,
                })

            # vùng 1σ cho chart: VIX/16 (SPX/SPY) hoặc IV30
            try:
                if vol_index and sym in ("SPX", "SPY"):
                    lv["implied_move_pts"] = vol_index["spot"] / 16.0 / 100.0 * lv["spot"]
                elif chain.get("iv30"):
                    lv["implied_move_pts"] = (chain["iv30"] / 100.0
                                              / math.sqrt(252.0) * lv["spot"])
            except Exception:
                pass

            # vẽ chart ảnh (nếu matplotlib có && không bị tắt)
            image_file = None
            if not args.no_image:
                try:
                    import chart
                    png = chart.render(sym, lv, session_date, near_days=lv.get("near_days"))
                    image_file = f"gex_{sym}.png"
                    with open(image_file, "wb") as f:
                        f.write(png)
                    files.append((image_file, png))
                    print(f"[ok] Đã vẽ chart {image_file} ({len(png) / 1024:.0f} KB)")
                except ImportError:
                    print("[!] Không có matplotlib (pip install matplotlib) — gửi text.")
                except Exception as e:
                    print(f"[!] Vẽ chart lỗi ({e}) — gửi text.", file=sys.stderr)

            embed, text = build_report(chain, lv, session_date, image_file)

            # sinh trade plan (nếu symbol được cấu hình instrument)
            instrument = PLAN_INSTRUMENTS.get(sym)
            if instrument:
                try:
                    import trade_plan
                    vol = vol_index if sym in ("SPX", "SPY") else None
                    plan_lines, plan_fields = trade_plan.build(chain, lv, instrument, vol=vol)
                    text = text + "\n\n" + "\n".join(plan_lines)
                    embed["fields"].extend(plan_fields)
                except Exception as e:
                    print(f"[!] Sinh trade plan lỗi ({e})", file=sys.stderr)

            embeds.append(embed)
            texts.append(text)
            print()
            print(text)
            print()
        except Exception as e:
            print(f"[!] Lỗi với {sym}: {e}", file=sys.stderr)

    if not texts:
        print("[!] Không tính được underlying nào.", file=sys.stderr)
        return 1

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("\n\n---\n\n".join(texts) + "\n")
        print(f"[ok] Đã ghi báo cáo ra {args.out}")

    if args.dry_run:
        print("[dry-run] Chưa gửi Discord (muốn gửi thật: đặt DISCORD_WEBHOOK_URL).")
        return 0
    if not args.webhook:
        print("[!] Thiếu DISCORD_WEBHOOK_URL — chỉ in ra console.")
        return 0
    send_discord(args.webhook, embeds, files)
    return 0


if __name__ == "__main__":
    sys.exit(main())
