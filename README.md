# 🤖 GEX Bot — Gamma Levels + Trade Plan hàng ngày trước phiên Mỹ

Bot tự chạy **mỗi ngày ~5 phút trước giờ mở cửa PHI Mỹ (9:30 ET)**, tính Gamma Exposure từ options chain thật (data CBOE delayed — miễn phí, không cần API key) rồi gửi vào **Discord**:

- **SPX** (kèm **VIX spot + VIX GEX**) → bản tin phân tích định lượng cho SPY/ES
- **NDX** → gamma levels + **trade plan cho NQ/MNQ futures**
- **QQQ, SPY** → gamma levels + trade plan cho từng ticker

**Chi phí vận hành: $0** (GitHub Actions miễn phí; bot chỉ cần `pip install matplotlib` để vẽ chart ảnh).

---

## 📋 Báo cáo mỗi ngày gồm

Mỗi underlying nhận **1 embed kèm ảnh chart** (kiểu SpotGamma, dark theme) + **trade plan if/then**:

🖼️ **Ảnh chart**: bar chart ngang net GEX theo strike quanh spot — xanh = call gamma, đỏ = put gamma, nhãn $/1%; đường trắng đứt = **SPOT**, đường cam chấm = **GAMMA FLIP**; mũi tên nhấn **CALL WALL / PUT WALL**; hộp thông tin front expiry (0DTE).

📊 **Levels**:

| Mức | Ý nghĩa cách dùng nhanh |
|---|---|
| **Spot (close)** | Giá đóng cửa phiên gần nhất làm mốc tham chiếu |
| **Gamma Flip** | Điểm zero-gamma (toàn expiry). Spot **trên** flip → vùng gamma dương (giá hay bị "pin"); spot **dưới** flip → vùng gamma âm (biến động dễ bùng nổ) |
| **Call Wall / Put Wall** | Strike call/put GEX lớn nhất **trong cửa sổ gần hạn (≤ 9 ngày)** — kháng cự / hỗ trợ của phiên |
| **Net GEX** | Tổng GEX toàn expiry ($/1% move) — dương = regime giảm biến động, âm = regime tăng biến động |
| **Front expiry (0DTE)** | Net GEX + walls của kỳ hạn gần nhất |
| **OI magnet** | Strike có tổng open interest lớn nhất quanh spot — mứt "hút" giá |

📋 **Bản tin phân tích định lượng** [1]–[4] (cho mọi underlying; SPX/SPY dùng VIX thật):
- **[1] Biên độ dự kiến**: Rule of 16 từ **VIX spot CBOE** (fallback IV30) → ±% ≈ ±pts, vùng biến động kỳ vọng, có so với walls 0DTE
- **[2] VIX Regime & phòng hộ dealer**: bucket vol (THẤP/CHUẨN/CAO/STRESS) · **Net GEX VIX options** (dealer short/long gamma trên VIX) · Net GEX + vị trí spot so với Gamma Flip (phát hiện **Transition/Fragile** khi spot ép sát flip) · **DEX** (Delta Exposure, dòng tiền Bullish/Bearish — tín hiệu chính là DEX 0DTE)
- **[3] Cảnh báo phân kỳ giá–VIX**: tín hiệu gom put âm thầm, không FOMO
- **[4] 3 kịch bản intraday if/then** theo regime: base case (chop/nén), downside (gãy mốc → trượt về vùng thanh khoản), upside (reclaim flip / breakout + vol crush → hút về Call Wall) — mỗi kịch bản có chiến lược tham khảo
- **Ladder**: Put Wall → PW 0DTE → SPOT → CW 0DTE → Call Wall → Flip → OI magnet
- **Quản trị rủi ro**: điểm invalidate, whipsaw, size theo biên độ 1σ (NQ $20/pt · MNQ $2/pt · ES $50/pt)

Xem ví dụ thật: [`sample-report.md`](sample-report.md) · ảnh mẫu: [`gex_SPX.png`](gex_SPX.png) · [`gex_NDX.png`](gex_NDX.png) · [`gex_QQQ.png`](gex_QQQ.png) · [`gex_SPY.png`](gex_SPY.png)

---

## 📁 Cấu trúc

```
gex-bot/
├── gex_bot.py                       # logic bot: data CBOE, tính GEX, gửi Discord
├── chart.py                         # vẽ chart ảnh PNG (matplotlib, dark theme)
├── trade_plan.py                    # bản tin [1]–[4]: Rule of 16, VIX, DEX, kịch bản
├── sample-report.md                 # ví dụ báo cáo (chạy --dry-run)
├── gex_NDX.png / gex_QQQ.png / gex_SPY.png   # ảnh chart mẫu
└── .github/workflows/gex-daily.yml  # lịch chạy tự động GitHub Actions
```

---

## 🚀 Setup (khoảng 10 phút)

### Bước 1 — Tạo Discord webhook
1. Discord → server của bạn → **Server Settings → Integrations → Webhooks**
2. **New Webhook** → đặt tên (vd `GEX Bot`) → chọn channel muốn nhận (vd `#gex-daily`)
3. **Copy Webhook URL** (dạng `https://discord.com/api/webhooks/...`)

### Bước 2 — Tạo GitHub repo + secret
1. Tạo repo mới trên GitHub (Private cũng được — Actions miễn phí)
2. Vào repo → **Settings → Secrets and variables → Actions → New repository secret**:
   - **Name:** `DISCORD_WEBHOOK_URL`
   - **Secret:** dán URL webhook ở bước 1

### Bước 3 — Đẩy code lên
Push **toàn bộ nội dung thư mục `gex-bot/`** lên **root** của repo (`gex_bot.py` nằm ngay tầng đầu, thư mục `.github/` cũng ở root):

```bash
cd gex-bot
git init
git branch -M main
git add .
git commit -m "GEX bot"
git remote add origin https://github.com/<username>/<repo>.git
git push -u origin main
```

### Bước 4 — Chạy thử
1. Vào repo → tab **Actions** → chọn workflow **GEX Daily** → **Enable workflow**
2. **Run workflow** → chờ ~30 giây → kiểm tra kênh Discord

### Bước 5 — Tự động hàng ngày ✅
- Mùa **EDT** (khoảng T3–T11): tin nhắn đến ~**20:25 giờ VN** (9:25 ET)
- Mùa **EST** (khoảng T11–T3): tin nhắn đến ~**21:25 giờ VN**

Script tự biết giờ New York (DST), tự nghỉ cuối tuần + holiday NYSE (danh sách 2026–2027 có sẵn), không gửi trùng 2 lần/ngày.

---

## 💻 Chạy trên máy (không cần GitHub)

```bash
pip install matplotlib                               # chỉ cần khi vẽ chart ảnh
python gex_bot.py --dry-run                          # xem báo cáo + ảnh, không gửi
DISCORD_WEBHOOK_URL="https://..." python gex_bot.py --force   # gửi ngay (text + ảnh)
python gex_bot.py --symbols NDX QQQ TSLA             # đổi danh sách ticker
python gex_bot.py --max-expiry-days 5                # giới hạn expiry cho toàn bộ tính toán
python gex_bot.py --no-image                         # chỉ gửi text, không cần matplotlib
python gex_bot.py --out report.md                    # ghi thêm báo cáo ra file
```

Yêu cầu: Python ≥ 3.9 + `matplotlib` (bot tự fallback về text nếu thiếu). Trên **Windows** cần thêm `pip install tzdata`.

> CBOE hỗ trợ hầu hết ticker Mỹ có option (SPY, QQQ, TSLA, NVDA...) và index option **NDX/SPX** (tự thêm dấu `_` khi gọi API). Riêng **giá NQ futures** không lấy từ CME được (họ chặn scraping theo điều khoản) — dùng NDX + ghi chú basis.

---

## ⚙️ Tuỳ biến

Sửa các biến trong khối **CẤU HÌNH** đầu file `gex_bot.py`:

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `SYMBOLS` | `["SPX", "NDX", "QQQ", "SPY"]` | Danh sách underlying (SPX→ES/SPY, NDX→NQ) |
| `PLAN_INSTRUMENTS` | NDX→NQ/MNQ, QQQ, SPY→SPY/ES | Sinh trade plan cho symbol nào; xoá bớt để tắt plan |
| `NEAR_EXPIRY_DAYS` | `9` | Cửa sổ gần hạn dùng cho walls + chart (0DTE/weekly/monthly tuần hết hạn) |
| `MINUTES_BEFORE_OPEN` | `5` | Mục tiêu gửi trước giờ mở cửa bao nhiêu phút |
| `TOLERANCE_BEFORE_MIN` | `15` | Chấp nhận cron chạy sớm/trễ tối đa bao nhiêu phút |
| `STRIKE_WINDOW_PCT` | `5.0` | Chart chỉ gồm strike trong ±% so với spot |
| `CHART_ROWS` | `13` | Số dòng chart ASCII (bản text) |
| `NYSE_HOLIDAYS` | 2026–2027 | Cập nhật thêm mỗi năm |

---

## 🧠 Cách tính & quy ước

- **GEX mỗi option** = `gamma × OI × 100 × spot² × 0.01` (USD cho mỗi 1% di chuyển của spot)
- **Quy ước dấu "naive dealer"**: calls dương, puts âm (giả định dealer long call / short put — cùng cách tính của SqueezeMetrics)
- **Gamma Flip** (toàn expiry): điểm tổng GEX lũy kế đổi dấu âm → dương, nội suy tuyến tính
- **Call/Put Wall** (≤ 9 ngày): strike có net GEX dương lớn nhất / âm nhất trong cửa sổ gần hạn — tránh bị LEAPS/monthly xa làm lệch wall "hàng ngày"
- **OI magnet**: strike có tổng OI lớn nhất trong ±5% quanh spot
- **Implied move**: VIX/16 (Rule of 16, SPX/SPY dùng VIX spot CBOE) hoặc `IV30 / √252 × spot` (1σ)
- **DEX** (Delta Exposure): `Σ delta × OI × 100 × spot` (calls +, puts −) — DEX 0DTE là dòng tiền chủ đạo của phiên
- **VIX GEX**: net GEX trên VIX options chain — trạng thái gamma của dealer trên chính VIX
- **NQ futures ≈ NDX + basis** (thường +0.1% đến +0.5% tuỳ lãi suất/kỳ hạn) — các mứt NDX quy đổi gần đúng 1:1 sang NQ

## ⚠️ Lưu ý & giới hạn

- **Data delayed**: trước phiên, OI của chain chính là dữ liệu chốt phiên trước — đúng cơ sở của mọi GEX levels buổi sáng kiểu SpotGamma.
- **Số liệu sẽ khác các dịch vụ trả phí** — mỗi hãng mô hình hoá vị thế dealer khác nhau; đây là phương pháp "naive GEX" chuẩn của các free bot.
- **Trade plan là kịch bản tham khảo giáo dục** (if/then theo levels), KHÔNG phải khuyến nghị đầu tư — tự chịu trách nhiệm quyết định giao dịch của mình.
- **GitHub cron không đảm bảo đúng từng phút** (thường lệch 0–10 phút). Cần chính xác từng phút → chạy VPS bằng cron riêng.
- GEX chỉ là một thước đo positioning — dùng kèm price action, VWAP, tin tức v.v.
