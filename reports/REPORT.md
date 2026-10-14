# Lab 25B — Report

**Họ tên / nhóm:** Trần Chí Vĩ (MSV: 2A202602968)  **Commit:** `773006b0078851aa92fc31e86b8ef2c7ec5214c3`  **Điểm tự động (`make grade`):** 84.0 / 88 (88/88 với hidden SLO)

> Mọi nhận định phải có số. Dán số từ `reports/gameday/*.json`, `reports/workflows.json`, `reports/slo.json`.

## 1. Kết quả Game Day (naive vs của bạn)

| Băng | Client | availability | p95 (s) | amplification | retry_ratio | cost/turn | recovery (s) | PASS? |
|---|---|---:|---:|---:|---:|---:|---:|:---:|
| G1 | naive | 0.7194 | 7.157 | 7.738 | 6.738 | 2.163 | 4.0 | FAIL |
| G1 | yours | 1.0000 | 4.809 | 1.063 | 0.002 | 2.105 | 0.0 | PASS |
| G2 | naive | 0.4560 | 15.034 | 9.904 | 8.904 | 4.627 | 155.0 | FAIL |
| G2 | yours | 1.0000 | 10.955 | 1.049 | 0.006 | 2.143 | 0.0 | PASS |
| G3 | naive | 1.0000 | 4.768 | 1.164 | 0.164 | 2.328 | 0.0 | FAIL |
| G3 | yours | 1.0000 | 4.679 | 1.054 | 0.033 | 2.134 | 0.0 | PASS |
| G4 | naive | 1.0000 | 13.764 | 1.004 | 0.004 | 2.008 | 0.0 | FAIL |
| G4 | yours | 1.0000 | 5.573 | 1.013 | 0.002 | 2.061 | 0.0 | PASS |
| G5 | naive | 0.3699 | 5.359 | 17.030 | 16.030 | 2.124 | None | FAIL |
| G5 | yours | 0.8179 | 5.477 | 1.003 | 0.002 | 2.504 | None | PASS |
| G6 | naive | 0.5170 | 4.836 | 12.344 | 11.344 | 2.009 | 0.0 | FAIL |
| G6 | yours | 1.0000 | 6.026 | 1.055 | 0.019 | 2.208 | 0.0 | PASS |

## 2. Cấu hình client và lý do

| Tham số | Giá trị | Vì sao (dựa trên băng nào, số liệu nào) |
|---|---:|---|
| `attempt_timeout_s` | 7.5 | Băng G6: khi `alpha-eu` bị treo vô thời hạn (`outage: hang`), đặt timeout 7.5 s (thay vì 30 s) giúp client ngắt sớm và failover sang `beta`, bảo toàn availability ở mức 1.0000 và giữ thời gian phản hồi p95 trong tầm kiểm soát (6.026 s). |
| `max_attempts_per_provider` | 2 | Băng G2: tối đa 1 lần retry trên cùng provider (tổng 2 attempts). Khi provider sập quá tải 529, attempt 2 thất bại lập tức kích hoạt failover thay vì bám trụ retry gây lãng phí, giảm amplification từ 9.904 xuống 1.049. |
| `min_time_left_s` | 1.0 | Tránh lãng phí tài nguyên: nếu thời gian còn lại trước deadline 30 s không đủ cho một cuộc gọi LLM hoàn tất (service median của `alpha` là 1.2 s), client chủ động dừng sớm thay vì bắn request rồi bỏ rơi giữa chừng. |
| `backoff_base_s` / `cap_s` | 0.2 / 3.5 | Băng G1, G2: Exponential backoff kèm full jitter rải đều các lần retry trong khoảng [0, 3.5] s, giải phóng provider khỏi hiện tượng thundering herd và triệt tiêu bão retry storm. |
| `retry_budget_ratio` | 0.1 | Băng G3: khống chế số lượng retry không vượt quá 10% tổng request lần đầu, hạ `retry_ratio` của G3 từ 0.164 (naive) xuống 0.033 (đạt chuẩn <= 0.10). |
| `breaker.window_size` / `min_calls` | 20 / 10 | Băng G2, G5: Cửa sổ trượt 20 calls với ngưỡng tối thiểu 10 calls đảm bảo mẫu thống kê chuẩn xác, không bị ngắt oan do vài lỗi ngẫu nhiên trong giờ thấp điểm. |
| `breaker.failure_rate_threshold` | 0.5 | Băng G2: khi tỷ lệ lỗi vượt quá 50% (529 và timeout tăng cao), breaker lập tức OPEN để cô lập provider bị sập và chuyển hướng tải sang các node dự phòng. |
| `breaker.slow_call_s` | 4.8 | Băng G4: `alpha` suy thoái làm thời gian xử lý tăng thêm 5.0 s (~6.2 s). Ngưỡng 4.8 s nhận diện sớm các cuộc gọi chậm bất thường, hạ `latency_p95_s` từ 13.764 s xuống 5.573 s (vượt yêu cầu <= 10 s). |
| `breaker.slow_rate_threshold` | 0.55 | Băng G4: khi trên 55% call thành công nhưng bị trễ (>= 4.8 s), breaker chuyển mạch sang OPEN vì lý do `slow_call_rate`. |
| `breaker.open_duration_s` | 9.0 | Băng G2: giữ mạch OPEN trong 9.0 s để hàng đợi provider xả sạch các request tồn đọng trước khi chuyển sang HALF-OPEN thăm dò. |
| `breaker.half_open_max_probes` | 2 | Giới hạn tối đa 2 request probe đồng thời trong trạng thái HALF-OPEN nhằm ngăn tải lớn làm tái sập provider đang trong giai đoạn hồi phục. |

Sơ đồ quyết định của `on_error` (ASCII hoặc bảng: loại lỗi → ghi breaker? → retry? → failover? → cooldown?):

```
                       [Provider Error Received]
                                  |
                           classify(err)
                                  |
       +--------------------------+--------------------------+
       |                          |                          |
 [CLIENT_ERROR]           [QUOTA_EXHAUSTED]            [RATE_LIMITED]
  (400, 401, 422)               (429)                       (429)
       |                          |                          |
Release Permit             Release Permit             Release Permit
No Breaker Record          No Breaker Record          No Breaker Record
No Retry                   Cooldown 300s              Cooldown = retry_after
GiveUp("client_error")     Failover immediately      Failover immediately
                                  |
       +--------------------------+--------------------------+
       |
 [OVERLOADED / SERVER_ERROR / TIMEOUT]
  (529, 503, 500, 502, 504, 0/timeout)
       |
 Record to Breaker: on_failure()
       |
 Can retry on same provider?
   - tries < max_attempts (2)
   - Breaker NOT OPEN
   - Retry Budget available (tokens >= 1.0)
       |
    +--+--+
    |     |
  [YES]  [NO]
    |     |
    |   Failover to next compatible ladder step
    |   (idx += 1, tries = 0, delay = 0.0)
    |
 Calculate FullJitterBackoff.delay(tries, retry_after)
 Send to SAME provider after delay
```

## 3. Ba thí nghiệm (mỗi cái 3–5 câu + số)

1. **Retry budget là lớp phòng thủ cuối.** Giả lập breaker cấu hình sai (`breaker.min_calls: 1000000000`) và đặt `max_attempts_per_provider: 5`.
   - Khi bật retry budget (ratio = 0.1): ở G2, `amplification = 1.092`, `retry_ratio = 0.048`, `recovery_s = 0.0 s`. Hệ thống ổn định và bảo vệ hàng đợi provider an toàn.
   - Khi tắt retry budget: ở G2, `amplification` tăng vọt lên `3.842`, `retry_ratio = 1.482`, `recovery_s = 68.0 s`. Provider bị nghẽn hàng đợi kéo dài ngay cả khi phần cứng đã hồi phục.
   - *Kết luận:* Khi breaker hoạt động đúng, nó ngắt mạch nhanh ở cấp độ provider; tuy nhiên nếu breaker bị misconfigure (hoặc chưa đủ số call để trip), retry budget hoạt động như một cầu chì độc lập ở cấp độ toàn cục, khống chế số lượng retry không bao giờ vượt quá tỷ lệ token cho phép (10%), chặn đứng nguy cơ bão retry tự kích hoạt. Do đó, phòng thủ chiều sâu (defense-in-depth) bắt buộc phải duy trì cả hai.
2. **`attempt_timeout_s: 30`.** Chạy G6 (và G4 để đối chứng):
   - Ở G6 với timeout 7.5 s: `availability = 1.0000`, `latency_p95_s = 6.026 s`, `give_up_reasons: {}`.
   - Ở G6 với timeout 30.0 s: `availability` tụt xuống `0.9488`, `latency_p95_s = 5.612 s`, xuất hiện `give_up_reasons: {'deadline': 121}`.
   - *Phân tích:* `latency_p95_s` giảm từ 6.026 s xuống 5.612 s là biểu hiện rõ rệt của **survivorship bias**: các call đen đủi bị kẹt vào provider treo (`alpha-eu`) đã chiếm trọn 30 s deadline, khiến lượt đó thất bại hoàn toàn và bị loại khỏi tập thống kê p95. Ở G4, do provider chỉ chậm thêm 5.0 s (tổng ~6.2 s < 7.5 s và < 30.0 s) và không bị treo vĩnh viễn, nên p95 gần như không đổi.
   - *Timeout đúng:* Phải dựa trên `p99 bình thường + buffer an toàn` kết hợp với `deadline tổng của user chia cho số bậc failover`, nhằm đảm bảo client luôn còn đủ thời gian failover sang provider tiếp theo khi bậc hiện tại bị treo.
3. **Bỏ lọc tool calling khỏi `ladder()`.** Chạy G6.
   - Khi có lọc: `incompatible_routes = 0`, `availability = 1.0000`.
   - Khi bỏ lọc: `incompatible_routes = 244`, `availability` tụt xuống `0.8977`.
   - *Góc nhìn Product Manager:* "Khi cụm Provider A gặp sự cố sập nguồn và Provider B siết chặt quota, hệ thống đã ngớ ngẩn chuyển hướng 244 yêu cầu xử lý hoàn tiền của khách hàng sang model giá rẻ `gamma-mini` (vốn không hỗ trợ Tool Calling). Hậu quả là model sinh ra câu trả lời rác hoặc lỗi cú pháp thay vì gọi API thực hiện hoàn tiền. Đối với khách hàng, tính năng hoàn tiền bị tê liệt hoàn toàn, tỷ lệ chuyển đổi và độ tin cậy của dịch vụ tụt dốc nghiêm trọng, phát sinh 244 khiếu nại CSKH trong vòng vài phút."

## 4. Workflow hoàn tiền

| Executor | completion | crashes | duplicate refunds | duplicate emails | lost refunds | overpaid | wasted LLM / crash |
|---|---:|---:|---:|---:|---:|---:|---:|
| naive | 1.0 | 81 | 128 | 81 | 0 | 1496700 | 1.123 |
| yours | 1.0 | 66 | 0 | 0 | 0 | 0 | 0.303 |

**Trả lời:**
- *Vì sao chỉ idempotency key là chưa đủ?* Idempotency key bảo vệ API bên thứ ba không bị gọi trùng, nhưng nếu không có checkpoint, mỗi khi worker bị crash, worker mới sẽ phải chạy lại từ đầu toàn bộ các bước tính toán (classify, lookup). Điều này gây lãng phí lớn chi phí và thời gian gọi LLM (`wasted_llm_per_crash` ở naive lên tới 1.123 so với 0.303 của yours).
- *Vì sao chỉ checkpoint cũng chưa đủ?* Nếu worker gặp timeout mạng khi gọi payment (tiền đã trừ phía cổng thanh toán nhưng gói tin phản hồi bị rớt trên đường truyền), hoặc worker bị crash ngay sau khi tiền đã chuyển thành công nhưng trước khi kịp lưu checkpoint: khi restart, worker mới sẽ gọi lại bước thanh toán. Nếu không có idempotency key, payment gateway sẽ trừ tiền lần thứ hai, gây ra `duplicate_refunds` và thâm hụt tài chính (`overpaid_cents`).
- *Điều gì xảy ra nếu ghi checkpoint TRƯỚC khi gọi payment?* Nếu ghi checkpoint trước khi thực thi, trong trường hợp worker bị crash ngay sau khi lưu checkpoint nhưng trước khi gọi API thanh toán thành công: worker mới khi khởi động lại sẽ kiểm tra thấy checkpoint đã tồn tại và lầm tưởng bước hoàn tiền đã xong, do đó bỏ qua bước này. Kết quả là yêu cầu hoàn tiền bị biến mất vĩnh viễn (`lost_refunds`), gây mất mát nghiêm trọng cho quyền lợi khách hàng.

## 5. Alerting & error budget

| Sự cố | Mong đợi | Naive alerter | Burn-rate alerter | Độ trễ phát hiện |
|---|---|---|---|---:|
| checkout_bug | page | page | page | 27 min |
| provider_outage | page | page | page | 4 min |
| deploy_blip | none | page (false page) | none | - |
| slow_leak | ticket | none (undetected) | ticket | 1337 min (~22.3h) |
| degraded_evening | page | page | page | 291 min (~4.8h) |
| night_spike | none | page (false page) | none | - |
| small_bump | none | page (false page) | none | - |

**Error budget 30 ngày:**
- Tổng requests: `25,901,755`
- Số lỗi cho phép (`allowed_bad` ở SLO 99.5%): `129,508.8`
- Số lỗi thực tế (`bad`): `55,618`
- Availability đạt được: `99.7853%`
- Đã tiêu: `42.95%` (`budget_consumed: 0.4295`), còn lại: `57.05%` (`budget_remaining: 0.5705`).
- **Có đóng băng release không?** **KHÔNG**, vì ngân sách lỗi còn lại 57.05% (> 0), hệ thống vẫn đang vận hành an toàn trên mục tiêu SLO 99.5%.
- **Vì sao `small_bump` (20 phút ở 12% lỗi) không nên đánh thức ai lúc 11 giờ trưa?**
  Trong 20 phút với tỷ lệ lỗi 12%, lượng lỗi phát sinh chỉ chiếm khoảng `20 * (0.12 - 0.005) / (43200 * 0.005) ≈ 1.06%` tổng ngân sách lỗi của cả tháng. Sự cố này mang tính gián đoạn ngắn (transient blip), không có nguy cơ làm cháy cạn ngân sách tháng. Theo triết lý SRE, chỉ gửi `page` đánh thức kỹ sư on-call khi sự cố đe dọa tiêu hao một phần đáng kể error budget (ví dụ 2% trong 1 giờ hoặc 5% trong 6 giờ) và vẫn đang tiếp diễn. Gửi page cho một đợt sóng ngắn 20 phút chỉ gây alert fatigue (kiệt sức vì báo động giả).

## 6. Postmortem — Băng G2 với client naive (blameless)

*Dùng `make plot` (hình `reports/gameday/G2_overload_storm.png`) và CSV `reports/gameday/naive_G2_overload_storm_timeseries.csv`.*

**Tóm tắt (3 câu):**
Vào thời điểm t=120 s của kịch bản G2, deployment chính `alpha` bị sụt giảm đột ngột 75% năng lực GPU trong vòng 3 phút. Do client phiên bản v1 (`NaiveClient`) sử dụng 3 tầng retry lồng nhau với delay cố định và không có circuit breaker, lượng request retry bùng phát dữ dội lên tới 184 attempts/s khiến hàng đợi provider nghẽn hoàn toàn. Hệ thống rơi vào trạng thái lỗi siêu bền (metastable failure), không thể tự hồi phục trong suốt 155 giây sau khi hạ tầng GPU của provider đã trở lại 100% năng lực.

**Ảnh hưởng:**
Có tổng cộng `1,285` lượt người dùng bị thất bại (availability chỉ đạt `45.60%`), thời gian gián đoạn kéo dài `335 s` (từ t=120 s đến t=455 s). Trong khoảng thời gian 10 phút diễn ra sự cố, lượng lỗi này đã làm bốc hơi hơn `10,700%` ngân sách lỗi cho phép của cửa sổ 10 phút, làm lãng phí `34,475` attempts gọi API và vi phạm `28,381` lần khuyến cáo `retry-after`.

**Timeline** (giây ảo — lấy từ CSV `reports/gameday/naive_G2_overload_storm_timeseries.csv`):

| t | Sự kiện | Bằng chứng (số) |
|---:|---|---|
| 110 | Trạng thái bình thường trước sự cố | `attempts_alpha: 5`, `calls_ok: 9`, `calls_done: 9` (tỷ lệ thành công 100%). |
| 120 | Provider `alpha` mất 75% năng lực xử lý (GPU capacity factor = 0.25) | `attempts_alpha: 5`, `calls_ok: 7`, `calls_done: 7`. Năng lực phục vụ giảm từ 20 rps xuống ~5 rps; hàng đợi bắt đầu ứ đọng. |
| 150 | Retry storm bùng phát dữ dội; hệ thống tê liệt hoàn toàn | `attempts_alpha: 163`, `calls_ok: 0`, `calls_done: 3`. 3 tầng retry không jitter nhân tải lên gấp 30 lần, vượt xa sức chứa của server. |
| 250 | Đỉnh điểm bão retry | `attempts_alpha: 184`, `calls_ok: 0`, `calls_done: 6`. Toàn bộ request mới bị từ chối 529 hoặc rớt vì hàng đợi chờ quá 10 s. |
| 300 | Nhà cung cấp khôi phục 100% GPU | `attempts_alpha: 115`, `calls_ok: 0`, `calls_done: 5`. Mặc dù phần cứng đã hồi phục, tỷ lệ thành công của user vẫn là 0%. |
| 350 | Tình trạng metastable failure tiếp diễn | `attempts_alpha: 17`, `calls_ok: 0`, `calls_done: 4`. Hàng đợi server vẫn ngập trong các request rác mà client đã timeout bỏ cuộc từ lâu. |
| 455 | Bắt đầu hồi phục sau 155 giây kể từ khi hạ tầng khôi phục | `attempts_alpha: 13`, `calls_ok: 4`, `calls_done: 5`. Server hoàn tất việc xả hàng đợi tồn đọng. |
| 460 | Hệ thống chính thức ổn định trở lại | `attempts_alpha: 14`, `calls_ok: 9`, `calls_done: 12`. Tỷ lệ thành công quay lại ngưỡng bình thường. |

**Trigger vs root cause:**
- **Trigger:** Nhà cung cấp dịch vụ hạ tầng `alpha` gặp sự cố phần cứng, tạm thời mất 75% GPU trong thời gian 180 s (t=120..300).
- **Root cause:** Thiết kế client v1 thiếu hoàn toàn các cơ chế kiểm soát lưu lượng: 3 tầng retry lồng nhau (App × Gateway × SDK) tạo ra hệ số khuếch đại tối đa 36 attempts/call; delay cố định không có full jitter gây đồng bộ hóa tải; không tôn trọng header `retry-after`; và không có circuit breaker cũng như retry budget.
- **Vòng lặp Metastable Failure:** Tại sao provider phục hồi ở t=300 mà hệ thống vẫn sập đến t=455?
  Mô hình hàng đợi FIFO của provider tiếp tục tính toán và xử lý các request cũ ngay cả khi client đã hết kiên nhẫn và timeout (`client_timeout`). Khi GPU hồi phục ở t=300, toàn bộ tài nguyên máy chủ bị lãng phí để giải quyết các "request ma" đã quá hạn này. Trong lúc đó, các request mới đến tiếp tục phải chờ trong hàng đợi, bị timeout và lại sinh ra hàng loạt đợt retry mới. Vòng lặp phản hồi dương này (Work Amplification Feedback Loop) tự duy trì tải ảo cao hơn năng lực xử lý thực tế của server suốt 155 giây.

**Điều gì giúp / điều gì làm tệ hơn:**
- *Làm tệ hơn:* Client retry liên tục với tần suất cực cao (lên tới 184 req/s); không tôn trọng `retry-after` (28,381 vi phạm); không có circuit breaker để ngắt tải; tiếp tục dồn toàn bộ lưu lượng vào `alpha` mà không failover sang `alpha-eu` hay `beta`.
- *Điều giúp giải quyết (trong ResilientClient):*
  1. Circuit Breaker ngắt mạch OPEN ngay khi tỷ lệ lỗi vượt 50%, chuyển hướng toàn bộ traffic sang `alpha-eu`/`beta`, để yên cho `alpha` xả sạch hàng đợi.
  2. Retry Budget khống chế lượng retry tối đa 10% lưu lượng.
  3. Exponential backoff với full jitter và tôn trọng triệt để `retry-after`.
  4. Cơ chế HALF-OPEN giới hạn 2 probe request, tránh làm sập lại `alpha` khi vừa mở cửa.

**Action items** (cụ thể, có owner, đo được):

| # | Hành động | Loại (prevent / detect / mitigate) | Owner | Cách kiểm chứng |
|---|---|---|---|---|
| 1 | Thay thế toàn bộ `NaiveClient` bằng `ResilientClient` có Circuit Breaker sliding-window và Retry Budget 10%. | Mitigate | SRE Team / Trần Chí Vĩ | Chạy CI test mô phỏng kịch bản G2: `amplification <= 1.2`, `recovery_s <= 30 s`, `availability >= 99%`. |
| 2 | Thiết lập hệ thống Multi-window Burn-rate Alerting theo chuẩn SRE Workbook ch. 5 (cửa sổ 1h & 6h). | Detect | Monitoring Lead / Trần Chí Vĩ | Kiểm thử trên bộ dữ liệu `data/sli_30d.csv`: 0 false page, phát hiện sự cố outage trong vòng <= 5 phút. |
| 3 | Tích hợp deadline propagation & context cancellation truyền xuống provider/gateway (gửi header drop request khi client đã hủy). | Prevent | Backend Platform Team / Trần Chí Vĩ | Unit test xác nhận khi client hủy request, server lập tức hủy bỏ task khỏi hàng đợi xử lý. |
| 4 | Bổ sung bài test Game Day định kỳ tự động chạy trong pipeline CI/CD trước mỗi bản release lớn. | Prevent | QA Automation / Trần Chí Vĩ | Pipeline tự động chạy `make grade` đạt tối thiểu 88/88 điểm trước khi merge vào nhánh `main`. |

## 7. Một điểm yếu còn lại

**Điểm yếu còn lại:**
Trong kịch bản suy thoái GPU kéo dài hoặc Provider chính gặp sự cố (như G2 hoặc G6), khi Circuit Breaker chuyển hướng lưu lượng sang các provider dự phòng (`alpha-eu` và `beta`), mặc dù availability đạt 100%, độ trễ `p95` tăng đáng kể (ở G2 là `10.955 s`, ở G6 là `6.026 s`) và chi phí vận hành tăng vọt (do `beta` có chi phí 1.4/call và service median lên tới 1.8 s so với 1.2 s của `alpha`). Nếu tình huống xảy ra trên diện rộng và cả 4 provider cùng bị suy thoái mạng (slow region), client sẽ lần lượt thử và chờ timeout trên từng bậc thang fallback ladder, khiến tổng thời gian chờ của người dùng tiến sát deadline 30 s.

**Cách khắc phục:**
1. **Hedging / Speculative Execution có kiểm soát:** Đối với các lượt quan trọng, nếu sau p90 thời gian thông thường (~2.0 s) mà provider chính chưa phản hồi, client có thể chủ động bắn thêm một request song song (hedged request) sang `alpha-eu` và lấy kết quả của bên nào về trước, hủy bỏ request còn lại.
2. **Adaptive Concurrency Limit & Cost-aware Failover:** Giám sát độ trễ thời gian thực theo thuật toán Vegas/Varnish để điều tiết tải linh hoạt sang `gamma-mini` cho các tác vụ thuần chat (chi phí chỉ 0.2 và tốc độ 0.6 s/req), tối ưu hóa cả chi phí lẫn p95 latency.

## 8. Cache (Part G)

Số từ `reports/cache/*.json` (`make cache`).

| Client | availability | faq_availability | cache_hit_ratio | stale_served | wrong_answers | outdated_answers | cost/turn | PASS? |
|---|---:|---:|---:|---:|---:|---:|---:|:---:|
| resilient (không cache) | 0.8394 | 0.8442 | 0.000 | 0 | 0 | 0 | 1.387 | FAIL |
| cached (của bạn) | 0.9127 | 0.9645 | 0.645 | 160 | 0 | 11 | 0.920 | PASS |

| Tham số `cache:` | Giá trị | Vì sao (số liệu nào) |
|---|---:|---|
| `similarity_threshold` | 0.91 | Ở ngưỡng 0.80, cache bị trả nhầm 157 câu hỏi khác intent (`wrong_answers: 157`), do khoảng cách ngữ nghĩa giữa các cặp near-miss (như hỏi chính sách hoàn tiền vs thời gian nhận tiền hoàn) có cosine ~0.86. Đặt ngưỡng 0.91 loại bỏ hoàn toàn câu trả lời sai (`wrong_answers = 0`) mà vẫn duy trì `cache_hit_ratio` đạt 0.645 (vượt xa trần yêu cầu >= 0.50). |
| `ttl_s` | 50 | Khi bảng phí ship thay đổi âm thầm tại t=480 s (không có webhook thông báo), chỉ có TTL mới giúp hết hạn câu trả lời cũ. Đặt TTL = 50 s giới hạn số câu trả lời lỗi thời chỉ là 11 câu (`outdated_answers = 11 <= 15`), trong khi TTL = 300 s để lọt tới 95 câu lỗi thời. |
| `max_stale_s` | 300 | Sự cố mất kết nối toàn bộ provider tại C1 kéo dài 120 s (t=240..360 s). Cửa sổ stale 300 s (> 120 s) cho phép cơ chế stale-if-error cứu được 160 lượt hỏi FAQ trong đợt sập, nâng `faq_availability` lên 96.45% (vượt xa chỉ tiêu 93%). |

Thí nghiệm (mỗi cái 2–4 câu + số):

1. **Ngưỡng.** Chạy C1 với `similarity_threshold` = 0.80 / 0.91 / 0.97:
   - Ngưỡng 0.80: `cache_hit_ratio = 0.666`, `wrong_answers = 157` (`wrong_answers_by_reason: {'other_question': 157}`), `cost = 2320.4`.
   - Ngưỡng 0.91: `cache_hit_ratio = 0.645`, `wrong_answers = 0`, `cost = 2351.2`.
   - Ngưỡng 0.97: `cache_hit_ratio = 0.581`, `wrong_answers = 0`, `cost = 2468.0`.
   - *Phân tích:* Ở ngưỡng 0.80, các câu hỏi có ý định khác nhau nhưng từ ngữ gần giống nhau (như "Chính sách hoàn tiền thế nào?" vs "Bao lâu thì nhận được tiền hoàn?", hoặc "Đổi size áo" vs "Đổi màu sản phẩm") bị nhận diện nhầm, dẫn đến trả lời sai cho 157 khách hàng. Ngưỡng 0.91 là điểm cân bằng tối ưu: triệt tiêu hoàn toàn câu trả lời sai mà không làm giảm hit ratio quá mức như ngưỡng 0.97.
2. **TTL.** Chạy với `ttl_s` = 30 / 50 / 300:
   - TTL 30 s: `outdated_answers = 2`, `cache_hit_ratio = 0.592`.
   - TTL 50 s: `outdated_answers = 11`, `cache_hit_ratio = 0.645`.
   - TTL 300 s: `outdated_answers = 95`, `cache_hit_ratio = 0.731`.
   - *Phân tích:* Webhook chỉ hoạt động khi hệ thống nguồn (CMS) chủ động phát sự kiện thay đổi. Khi bên vận chuyển đổi biểu phí âm thầm (silent update), client hoàn toàn không nhận được webhook nên cache không thể chủ động invalidate. Do đó, TTL là chốt chặn duy nhất làm hết hạn entry cũ. TTL = 50 s giữ số câu trả lời cũ ở mức 11 (rất an toàn so với trần 15).
3. **Stale-if-error.** Chạy với `max_stale_s: 0` vs `300`:
   - Khi `max_stale_s = 0`: `faq_availability = 0.8733` (FAIL yêu cầu >= 0.93), `stale_served = 0`.
   - Khi `max_stale_s = 300`: `faq_availability = 0.9645` (PASS), `stale_served = 160`.
   - *Phân tích:* Trong 2 phút toàn bộ provider ngừng hoạt động, nếu không có stale-if-error, toàn bộ câu hỏi FAQ đã hết hạn TTL sẽ lập tức thất bại. Việc cho phép trả câu trả lời cũ nhưng an toàn giúp cứu sống 160 lượt truy vấn và đưa availability tăng thêm 9.12%.

**Trả lời:**

- **Vì sao câu hỏi cá nhân ("Đơn #48213 của tôi đang ở đâu?") KHÔNG được cache, kể cả khi cache theo từng user?**
  1. *Dữ liệu biến động theo thời gian thực:* Trạng thái đơn hàng, tiến độ hoàn tiền hay điểm thưởng thay đổi liên tục theo từng sự kiện vận chuyển hoặc giao dịch. Cache câu trả lời cá nhân sẽ khiến khách hàng nhận thông tin cũ, gây hiểu lầm nghiêm trọng (ví dụ: đơn đã giao nhưng cache vẫn báo đang xử lý).
  2. *Rủi ro rò rỉ dữ liệu (Privacy Leak):* Các câu hỏi cá nhân giữa các user thường có cấu trúc câu gần giống hệt nhau (chỉ khác mã đơn hàng) nên độ tương đồng vector lên tới ~0.99. Nếu sơ suất trong việc cô lập ngữ cảnh (scope), cache sẽ trả nhầm thông tin đơn hàng/tiền bạc của user A cho user B (`wrong_answers: other_user`), vi phạm nghiêm trọng luật bảo vệ dữ liệu cá nhân (GDPR/Nghị định 13).
- **Trong lúc mọi provider sập, dashboard vẫn xanh nhờ cache. Bạn sẽ alert dựa trên số nào để on-call vẫn biết có sự cố?**
  Dashboard tổng thể có thể vẫn hiển thị tỷ lệ thành công 200 OK cao do cache đã gánh tải. Để on-call phát hiện ra sự cố ở tầng provider bên dưới, cần thiết lập cảnh báo dựa trên các chỉ số:
  1. **Tỷ lệ lỗi ở tầng Origin / Upstream:** Giám sát trực tiếp tỷ lệ 5xx/timeout từ gateway kết nối tới các LLM provider (chỉ số trước khi qua cache).
  2. **Tỷ lệ phục vụ Stale (`stale_served` rate):** Đặt alert khi tốc độ phục vụ câu trả lời cũ (`stale_served / min`) tăng đột biến vượt ngưỡng 0.
  3. **Lưu lượng hoặc tỷ lệ thất bại của non-cacheable traffic:** Giám sát riêng availability của các lượt hỏi thông tin cá nhân (`personal=True`) và các lượt yêu cầu tool calling — vì các lượt này bắt buộc phải đi thẳng tới provider nên sẽ phản ánh chính xác tình trạng sức khỏe của hạ tầng bên dưới.
