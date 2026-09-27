# -*- coding: utf-8 -*-
"""
Sinh BẢN TIN PHÂN TÍCH ĐỊNH LƯỢNG mỗi sáng — cấu trúc [1]–[4]:
  [1] Biên độ dao động dự kiến (Rule of 16 từ VIX spot; fallback IV30)
  [2] VIX Regime & trạng thái phòng hộ dealer (Net GEX, Gamma Flip, DEX)
  [3] Cảnh báo phân kỳ giá–vol (giới hạn: snapshot tĩnh trước phiên)
  [4] Kịch bản intraday if/then: walls 0DTE, cụm GEX dương, vùng thanh khoản

Thuật toán:
  - Rule of 16: biên độ 1 ngày ≈ VIX/16 (%); fallback IV30/√252 (toán tương đương)
  - DEX (Delta Exposure) = Σ delta × OI × 100 × spot (calls +, puts − theo dấu delta)
    DEX 0DTE/front expiry = dòng tiền chủ đạo của phiên; DEX toàn expiry chỉ để tham khảo
  - Regime: spot TRÊN flip → dealer long gamma (nén giá); DƯỚI flip → short gamma (thổi vol)
  - Transition/Fragile: spot nằm trong khoảng hẹp quanh Gamma Flip
  - Cụm GEX dương phía trên / vùng thanh khoản phía dưới: top strikes theo |net GEX|
    trong cửa sổ gần hạn (gex_bot truyền vào)

LƯU Ý: công cụ tham khảo giáo dục, không phải khuyến nghị đầu tư.
"""
from __future__ import annotations

import math

WINDOW_PCT = 5.0    # cửa sổ tìm OI magnet quanh spot
FRAGILE_PCT = 0.3   # khoảng cách spot–flip (%) dưới ngưỡng này → Transition/Fragile
WATCH_PCT = 1.0     # dưới ngưỡng này → "theo dõi flip sát"
MERGE_PCT = 0.25    # Put Wall & Flip cách nhau dưới ngưỡng này (%) → mốc trùng


def _lvl(k: float) -> str:
    if k >= 1000:
        return f"{k:,.0f}"
    if abs(k - round(k)) < 0.005:
        return f"{k:g}"
    return f"{k:,.2f}"


def _fmt_usd(v: float) -> str:
    a = abs(v)
    if a >= 1e12:
        s = f"${a / 1e12:.2f}T"
    elif a >= 1e9:
        s = f"${a / 1e9:.2f}B"
    elif a >= 1e6:
        s = f"${a / 1e6:.1f}M"
    elif a >= 1e3:
        s = f"${a / 1e3:.0f}K"
    else:
        s = f"${a:.0f}"
    return ("+" if v >= 0 else "-") + s


def _vix_regime(v: float) -> tuple[str, str]:
    if v < 15:
        return ("THẤP (<15)", "premium rẻ — thiên về mua options/debit; bán premium chỉ chọn vùng")
    if v <= 25:
        return ("CHUẨN (15–25)", "premium trung bình — hợp bán premium (credit spread) có kiểm soát")
    if v <= 35:
        return ("CAO (25–35)", "premium đắt — thận trọng bán premium, ưu tiên cấu trúc long vol")
    return ("STRESS (>35)", "vol cực đại — quản trị rủi ro là ưu tiên tuyệt đối")


def build(chain: dict, lv: dict, instrument: str,
          vol: dict | None = None) -> tuple[list[str], list[dict]]:
    """Trả về (dòng text markdown, danh sách field cho Discord embed).

    vol: {"spot": VIX, "net_gex": ...} nếu lấy được VIX spot từ CBOE.
    """
    sym = chain["symbol"]
    spot = lv["spot"]
    flip = lv["gamma_flip"]
    cw, pw = lv["call_wall"], lv["put_wall"]
    front = lv["front"]
    iv30 = chain.get("iv30")
    net = lv["net_by_strike"]
    total_dex = lv.get("total_dex") or 0.0
    front_dex = lv.get("front_dex") or 0.0

    # ---- [1] biên độ dự kiến: Rule of 16 ----
    if vol:
        vol_src = f"VIX spot {vol['spot']:.2f} (CBOE)"
        move_pct = vol["spot"] / 16.0
        regime_v = vol["spot"]
    elif iv30:
        vol_src = f"IV30 {iv30:.1f} (không có VIX spot — dùng IV30)"
        move_pct = iv30 / 100.0 / math.sqrt(252.0)
        regime_v = iv30
    else:
        vol_src, move_pct, regime_v = None, None, None
    pts = (move_pct / 100.0) * spot if move_pct else None

    # ---- [2] regime & dealer ----
    positive = flip is None or spot >= flip
    if flip is not None:
        dist_pct = abs(spot - flip) / spot * 100.0
        fragile = dist_pct < FRAGILE_PCT
        watch = dist_pct < WATCH_PCT
        regime_short = "POSITIVE GAMMA" if positive else "NEGATIVE GAMMA"
        flip_pos = f"spot {'TRÊN' if positive else 'DƯỚI'} flip {_lvl(flip)} ({dist_pct:.2f}%)"
    else:
        fragile, watch = False, False
        regime_short = "POSITIVE GAMMA (flip N/A)"
        flip_pos = "không xác định được flip"
    frag_tag = " · ⚡ TRANSITION/FRAGILE" if fragile else (" · theo dõi flip sát" if watch else "")

    # DEX: dòng tiền 0DTE là tín hiệu chủ đạo của phiên
    flow_dex = front_dex if front_dex else total_dex
    dex_tilt = ("Bearish" if flow_dex < 0 else "Bullish") if flow_dex else "Trung tính"
    dex_note = (f"DEX toàn expiry {_fmt_usd(total_dex)} · DEX 0DTE/front {_fmt_usd(front_dex)} "
                f"→ dòng tiền chủ đạo thiên hướng {dex_tilt}")

    # ---- mốc then chốt theo regime ----
    fpw = front["put_wall"] if front else None
    if positive:
        key_level = flip if flip is not None else (fpw or pw)
    else:
        key_level = fpw or pw
    merge_note = ""
    if front and flip is not None and fpw is not None:
        if abs(fpw - flip) / spot * 100.0 < MERGE_PCT:
            merge_note = (f"Put Wall 0DTE & Gamma Flip gần trùng nhau tại ~{_lvl(flip)} "
                          f"— mốc then chốt của phiên")
            if positive:
                key_level = (fpw + flip) / 2.0

    # ---- cụm GEX dương phía trên / vùng thanh khoản phía dưới ----
    clusters = sorted((k for k, v in net.items() if v > 0 and k > spot),
                      key=lambda k: net[k], reverse=True)[:3]
    clusters.sort()
    zones = sorted((k for k in net if k < spot), key=lambda k: abs(net[k]), reverse=True)[:2]
    zones.sort()
    clusters_txt = " / ".join(_lvl(k) for k in clusters) or "không có — price discovery"
    zones_txt = " / ".join(_lvl(k) for k in zones) or "không có — hỗ trợ trống"

    # ---- OI magnet ----
    oi_by: dict[float, float] = {}
    for r in chain["rows"]:
        if abs(r["strike"] / spot - 1.0) * 100.0 > WINDOW_PCT:
            continue
        oi_by[r["strike"]] = oi_by.get(r["strike"], 0.0) + r["oi"]
    magnet = max(oi_by, key=oi_by.get) if oi_by else None

    # ---- ladder ----
    info: dict[float, tuple[str, str]] = {}
    if magnet is not None:
        info[magnet] = ("OI magnet", "strike OI lớn nhất quanh spot — mứt 'hút' giá")
    info[pw] = ("Put Wall", "hỗ trợ / magnet phía dưới")
    info[cw] = ("Call Wall", "kháng cự / target phía trên")
    if flip is not None:
        info[flip] = ("Gamma Flip", "lằn ranh regime — mức invalidate bias")
    if front:
        if front["put_wall"] not in (pw,):
            info.setdefault(front["put_wall"], ("Put Wall 0DTE", "hỗ trợ của kỳ hạn front"))
        if front["call_wall"] not in (cw,):
            info.setdefault(front["call_wall"], ("Call Wall 0DTE", "kháng cự của kỳ hạn front"))
    ladder = list(info.items())
    if not any(abs(spot - v) < 0.01 for v in info):
        ladder.append((spot, ("SPOT", "close phiên trước")))
    ladder.sort()

    res_above = [v for v, _ in ladder if v > spot + 0.01]
    nearest_res = min(res_above) if res_above else None
    above_cw = [k for k in clusters if k > cw]
    ext_txt = _lvl(min(above_cw)) if above_cw else "price discovery"

    # ---- hệ quả phòng hộ ----
    if positive:
        hedge = (f"giữ trên {_lvl(key_level)} → dealer long gamma, mua khi giảm để neo giá; "
                 f"mất {_lvl(key_level)} → lật short gamma, buộc bán futures hedge, tạo áp lực "
                 f"cộng hưởng đẩy giá xuống sâu")
    else:
        hedge = (f"dealer đang short gamma — hỗ trợ quan trọng là {_lvl(key_level)}; "
                 f"chỉ khi lấy lại flip {_lvl(flip)} thị trường mới trở lại trạng thái nén giá "
                 f"(long gamma)" if flip is not None else
                 f"dealer đang short gamma — hỗ trợ quan trọng là {_lvl(key_level)}")

    # ---- [4] kịch bản theo regime ----
    kl = _lvl(key_level)
    if positive:
        sc_full = [
            (f"Kịch bản 1 (Base case — Nén giá): giá giữ trên {kl} → dealer long gamma neo giá, "
             f"giá chop/sideway rồi bò lên thử cụm GEX dương {clusters_txt}. Chiến lược tham khảo: "
             f"chỉ bán put premium dưới hỗ trợ khi giá phản ứng tốt, giảm quy mô lệnh."),
            (f"Kịch bản 2 (Downside — Gãy mốc then chốt): giá xuyên thủng dứt khoát {kl} → dealer "
             f"lật short gamma, lực bán hedge cộng hưởng với biên độ Rule of 16 đẩy giá trượt "
             f"nhanh về vùng thanh khoản {zones_txt}. Chiến lược tham khảo: hạn chế bắt đáy "
             f"chặn đầu bò; đứng ngoài hoặc short ngắn hạn thuận dòng chảy."),
            (f"Kịch bản 3 (Upside — Short squeeze): giá hấp thụ xong lực bán và bứt phá "
             + (f"qua {_lvl(nearest_res)}" if nearest_res else "kháng cự gần nhất")
             + (", VIX giảm (vol crush)" if vol else ", IV giảm")
             + f" → dealer mua lại futures, giá hút nhanh về Call Wall {_lvl(cw)}; phá vỡ mốc "
             f"này squeeze tiếp lên {ext_txt}."),
        ]
        sc_compact = [
            f"1️⃣ Giữ {kl} → chop, bò lên thử {clusters_txt}",
            f"2️⃣ Mất {kl} → short gamma, trượt về {zones_txt}",
            f"3️⃣ Phá " + (f"{_lvl(nearest_res)}" if nearest_res else "kháng cự")
            + f" + VIX giảm → hút về CW {_lvl(cw)}",
        ]
    else:
        reclaim = f"lấy lại flip {_lvl(flip)}" if flip is not None else "đảo chiều kết cấu"
        sc_full = [
            (f"Kịch bản 1 (Base case — Chop quanh hỗ trợ): giá giữ trên {kl} → còn hỗ trợ cơ học "
             f"tạm thời nhưng regime short gamma nên biên độ dễ nở bất ngờ; giá dao động giữa "
             f"{kl} và " + (f"{_lvl(nearest_res)}" if nearest_res else "kháng cự gần nhất")
             + f". Chiến lược tham khảo: giảm size, ưu tiên phản ứng nhanh quanh mốc, không giữ "
             f"vị thế dài trong day."),
            (f"Kịch bản 2 (Downside — Thả trôi): giá mất {kl} → không còn hỗ trợ gamma đáng kể, "
             f"lực bán hedge đẩy giá trượt nhanh về vùng thanh khoản sâu {zones_txt}. Chiến lược "
             f"tham khảo: hạn chế bắt đáy chặn đầu bò; đứng ngoài hoặc short ngắn hạn thuận dòng."),
            (f"Kịch bản 3 (Upside — Reclaim flip): giá {reclaim} → trạng thái lật lại long gamma, "
             f"dealer mua lại futures, giá hút nhanh về Call Wall {_lvl(cw)}; phá vỡ mốc này "
             f"squeeze tiếp lên {ext_txt}."),
        ]
        sc_compact = [
            f"1️⃣ Giữ {kl} → chop nhưng regime short gamma — giảm size",
            f"2️⃣ Mất {kl} → trượt nhanh về {zones_txt}",
            f"3️⃣ {reclaim.capitalize()} → lật long gamma, hút về CW {_lvl(cw)}",
        ]

    # ---- [3] cảnh báo phân kỳ ----
    div_note = (f"Snapshot tĩnh trước phiên nên chưa đánh giá được dòng chảy tương quan trong "
                f"phiên. Cảnh báo: nếu trong phiên {sym} tăng mà VIX cũng tăng (hoặc cả hai cùng "
                f"giảm bất thường) → dòng tiền đang âm thầm gom put phòng hộ, rủi ro đảo chiều "
                f"intraday rất cao — tuyệt đối không FOMO mua đuổi.")

    # ---- text markdown ----
    lines = [f"### 📋 BẢN TIN PHÂN TÍCH ĐỊNH LƯỢNG {sym} — trade plan {instrument}",
             "", "**[1] Biên độ dao động dự kiến hôm nay**"]
    if pts is not None:
        lo, hi = spot - pts, spot + pts
        lines += [
            f"- Nguồn vol: **{vol_src}**",
            f"- Rule of 16: biên độ dự kiến **±{move_pct:.2f}% ≈ ±{pts:,.1f} pts**",
            f"- Vùng biến động kỳ vọng: **[{lo:,.1f} – {hi:,.1f}]** (từ spot {spot:,.2f})",
        ]
        if front:
            cover = lo < front["put_wall"] and hi > front["call_wall"]
            lines.append(f"- Vùng này {'bao trùm' if cover else 'chưa bao trùm hết'} các chốt "
                         f"0DTE: Put Wall {_lvl(front['put_wall'])} – "
                         f"Call Wall {_lvl(front['call_wall'])}")
    else:
        lines.append("- Không có dữ liệu vol (VIX/IV30) — bỏ qua ước lượng biên độ.")

    lines += ["", "**[2] VIX Regime & phòng hộ dealer**"]
    if regime_v is not None:
        r_name, r_desc = _vix_regime(regime_v)
        lines.append(f"- VIX Regime: **{regime_v:.2f} — {r_name}**: {r_desc}")
    if vol and vol.get("net_gex") is not None:
        vgx = vol["net_gex"]
        lines.append(f"- Net GEX VIX options: **{_fmt_usd(vgx)}/1%** → dealer "
                     + ("SHORT gamma trên VIX — VIX dễ giật cục hơn bình thường" if vgx < 0 else
                        "long gamma trên VIX — VIX có xu hướng ổn định hơn"))
    lines += [
        f"- Trạng thái Gamma {sym}: Net GEX **{_fmt_usd(lv['total_gex'])}/1%** (toàn expiry) · "
        f"{flip_pos}{frag_tag}",
        f"- {dex_note}",
        f"- Hệ quả phòng hộ: {hedge}.",
    ]
    if merge_note:
        lines.append(f"- ⚡ {merge_note}")

    lines += ["", f"**[3] Tương quan / phân kỳ {sym}–VIX**", f"- {div_note}"]

    lines += ["", "**[4] Kịch bản intraday & cảnh báo rủi ro**"]
    if front:
        dte_tag = "0DTE" if front["dte"] == 0 else f"{front['dte']}DTE"
        lines.append(f"- Mốc front ({front['date']:%d/%m}, {dte_tag}): "
                     f"Spot {spot:,.2f} · Call Wall {_lvl(front['call_wall'])} · "
                     f"Put Wall {_lvl(front['put_wall'])}")
    lines.append(f"- {dex_note}")
    lines += [f"  {s}" for s in sc_full]
    lines += ["", "Ladder các mứt:"]
    lines += [f"  {_lvl(v):>9}  {name} — {role}" for v, (name, role) in ladder]
    lines += [
        "",
        "**Quản trị rủi ro:**",
        f"- Bias invalidated khi giá ĐÓNG PHIÊN xuyên {kl}"
        + ("" if positive else f" hoặc không {reclaim}"),
        "- Vào lệnh sát wall dễ whipsaw → chờ xác nhận price action",
    ]
    if pts is not None:
        lines.append(f"- Size theo biên độ ±{pts:,.0f} pts (1σ); NQ $20/pt · MNQ $2/pt · ES $50/pt"
                     if sym == "NDX" else
                     f"- Size theo biên độ ±{pts:,.0f} pts (1σ)")
    lines += ["", "_Tham khảo giáo dục — không phải khuyến nghị đầu tư._"]

    # ---- embed fields ----
    bias_val = f"**{regime_short}**{frag_tag}"
    if pts is not None:
        bias_val += (f"\nBiên độ: ±{move_pct:.2f}% ≈ ±{pts:,.0f} pts · "
                     f"vùng [{spot - pts:,.0f} – {spot + pts:,.0f}]")
    bias_val += f"\n{dex_note}"
    fields = [
        {"name": f"🎯 Bias — {instrument}", "inline": False, "value": bias_val},
    ]
    if vol and regime_v is not None:
        r_name, _ = _vix_regime(regime_v)
        vgx_txt = (f" · VIX GEX {_fmt_usd(vol['net_gex'])}/1%"
                   if vol.get("net_gex") is not None else "")
        fields.append({"name": "🧭 VIX Regime", "inline": False,
                       "value": f"VIX {vol['spot']:.2f} — {r_name}{vgx_txt}"})
    fields += [
        {"name": "📋 Kịch bản intraday", "inline": False, "value": "\n".join(sc_compact)},
        {"name": "⚠️ Phân kỳ giá–vol", "inline": False, "value": div_note},
        {"name": "🪜 Ladder", "inline": False,
         "value": "\n".join(f"`{_lvl(v):>9}` {name}" for v, (name, _) in ladder)},
    ]
    return lines, fields
