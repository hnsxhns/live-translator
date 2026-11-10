# Live Translator — app iPad qua TestFlight

App iPad gốc chứa sẵn toàn bộ Live Translator 1.8.0 (code + mô hình giọng nói, ~280 MB).
Mỗi người thử cài qua TestFlight và tự đăng nhập bằng API key Claude của mình — không ai dùng chung key.

GitHub tự build app trên máy Mac của GitHub mỗi khi nhánh `ipad-pwa` thay đổi
(workflow **iPad app (TestFlight)** trong tab *Actions* của repo `live-translator`):
chạy thử trên giả lập iPad, tạo file `.ipa`, và — khi đã có khoá Apple bên dưới — tự đẩy lên TestFlight.

## Việc cần làm một lần (chỉ chủ tài khoản Apple làm được, ~20 phút)

1. **Đăng ký Apple Developer Program** (99 USD/năm): https://developer.apple.com/programs/enroll/
   — đăng nhập Apple ID, chọn cá nhân (Individual). Apple duyệt từ vài giờ đến 1–2 ngày.
2. **Tạo Bundle ID**: https://developer.apple.com/account/resources/identifiers/list → nút **+** →
   *App IDs* → *App* → Description `Live Translator`, Bundle ID (Explicit) `com.hnsxhns.livetranslator` → Register.
3. **Tạo app trong App Store Connect**: https://appstoreconnect.apple.com/apps → **+** → *New App* →
   Platform **iOS**, Name `Live Translator` (nếu trùng tên thì thêm chữ, ví dụ `Live Translator VN`),
   Primary language Vietnamese, Bundle ID chọn `com.hnsxhns.livetranslator`, SKU `livetranslator` → Create.
4. **Tạo khoá API**: App Store Connect → *Users and Access* → *Integrations* → *App Store Connect API* →
   *Team Keys* → **+** → tên `GitHub`, quyền **Admin** → Generate.
   Tải file `AuthKey_XXXXXXXXXX.p8` (chỉ tải được 1 lần), ghi lại **Key ID** và **Issuer ID** (ở đầu trang).
5. **Team ID**: https://developer.apple.com/account → *Membership details* → *Team ID*.
6. **Dán vào GitHub** (bạn tự dán, đừng gửi khoá cho ai): https://github.com/hnsxhns/live-translator/settings/secrets/actions →
   *New repository secret*, tạo 4 secret:

   | Tên | Giá trị |
   |---|---|
   | `ASC_KEY_ID` | Key ID |
   | `ASC_ISSUER_ID` | Issuer ID |
   | `TEAM_ID` | Team ID |
   | `ASC_KEY_P8_BASE64` | nội dung file .p8 đã mã hoá base64 (lệnh dưới) |

   Lấy base64 của file .p8 trên Windows (PowerShell), rồi dán vào ô giá trị:

   ```powershell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("$HOME\Downloads\AuthKey_XXXXXXXXXX.p8")) | Set-Clipboard
   ```

7. Vào tab **Actions** của repo → **iPad app (TestFlight)** → **Run workflow** (nhánh `ipad-pwa`).
   Khoảng 20–30 phút sau, bản build xuất hiện trong App Store Connect → app → **TestFlight**.

## Mời người thử

App Store Connect → app → **TestFlight**:

- **Internal Testing** (tối đa 100 người trong nhóm tài khoản của bạn, không cần Apple duyệt): **+** → tạo nhóm →
  thêm người (họ phải được mời vào *Users and Access* trước).
- **External Testing** (tối đa 10.000 người, chỉ cần email hoặc link công khai): tạo nhóm → thêm build →
  điền thông tin kiểm thử → Apple duyệt bản đầu (thường < 1 ngày) → bật **Public Link** →
  được link dạng `https://testflight.apple.com/join/XXXXXXXX` để gửi cho mọi người.

Người thử: cài app **TestFlight** từ App Store → mở link mời → **Cài đặt** → mở Live Translator →
nhập API key Claude của chính họ (lấy ở platform.claude.com/settings/keys).

Bản build TestFlight dùng được 90 ngày; có bản mới, TestFlight tự báo người thử cập nhật.

## Ra bản mới

Sửa code trong `ipad/web`, tăng `BUILD` trong `ipad/web/js/version.js`, commit và push nhánh `ipad-pwa`.
GitHub tự build và đẩy bản mới lên TestFlight (số build = số lần chạy workflow).

## Không có tài khoản Apple Developer?

Mỗi lần chạy, workflow vẫn tạo file `LiveTranslator-unsigned-ipa` (trong phần *Artifacts* của lần chạy).
Có thể cài bằng **Sideloadly** trên Windows với Apple ID thường (cắm cáp iPad; app hết hạn sau 7 ngày).
