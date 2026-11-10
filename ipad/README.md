# Live Translator cho iPad

Bản web app (PWA) của Live Translator, đủ tính năng như bản máy tính **1.8.0**, cài lên iPad từ Safari —
không cần App Store, không cần máy Mac, không cần tài khoản Apple Developer.

| Tính năng | Bản máy tính | Bản iPad |
|---|---|---|
| Nghe tiếng Anh: chữ hiện ngay (Vosk) + sửa lại bằng Whisper base.en | ✓ | ✓ chạy ngay trên iPad (GPU nếu có) |
| Dịch tiếng Việt từng đoạn, thẳng hàng với tiếng Anh, mỗi câu một dòng | ✓ | ✓ |
| Nhận diện người nói (CAM++), Teacher sau 10 phút, đổi tên | ✓ | ✓ cùng mô hình, kết quả giống hệt |
| Ý chính cộng dồn theo mốc giờ | ✓ | ✓ |
| Khung Claude hai bên, đính kèm ảnh/PDF/Word/file chữ | ✓ | ✓ (ảnh HEIC tự chuyển sang JPEG) |
| 🎓 Learning: thư viện, Agenda, Documents, Summary, Exercises, Journal PDF, Certificate | ✓ | ✓ (chia sẻ qua bảng Chia sẻ của iPad) |
| Backup / Restore | ✓ | ✓ cùng định dạng file, dùng chéo được |
| Đăng nhập | API key, Google/email | API key |

## Các lệnh

```
cd ipad
npm install                 # lần đầu: tải thư viện
node build.mjs              # build bản mới vào site/v/<phiên bản>-<build>/
node serve.mjs              # thử trên máy tính: http://localhost:8787/
node deploy.mjs             # đưa lên GitHub Pages (HTTPS, miễn phí)
```

`build.mjs` lấy mô hình giọng nói từ thư mục `models/` của bản máy tính (`--models <thư mục>` nếu ở chỗ khác)
và tải Whisper từ Hugging Face ở lần build đầu.

## Phát hành bản mới (giống TestFlight)

1. Sửa code trong `web/`.
2. Tăng `BUILD` (và `APP_VERSION` nếu cần) trong `web/js/version.js`, ghi `NOTES` (ghi chú phiên bản) và `CHANNEL`
   (`beta` = chỉ người thử, `stable` = mọi người).
3. `node build.mjs` rồi `node deploy.mjs`.

Người dùng mở app sẽ thấy thanh **"Có bản mới … Cập nhật"**. Bấm vào số phiên bản ở thanh trên để:
xem ghi chú từng bản, mở lại một bản cũ, chọn nhận bản Beta hay chỉ bản Ổn định, gửi phản hồi,
và mở trang mời người thử có mã QR (`install.html`).

Bản cũ không bao giờ bị xoá: mỗi bản nằm ở `site/v/<phiên bản>-<build>/`, `build.mjs` từ chối ghi đè.
Muốn nhận phản hồi qua email: điền `feedback_email` trong `releases.json`.

## Cài lên iPad

1. Mở `https://<tên>.github.io/live-translator-ipad/install.html` bằng **Safari** (hoặc quét mã QR).
2. Nút **Chia sẻ** → **Thêm vào MH chính** → **Thêm**.
3. Mở **Live Translator** từ Màn hình chính, nhập API key, cho phép **Micro**.

Lần mở đầu app tải mô hình giọng nói (~150–230 MB, nên dùng Wi-Fi); sau đó lưu trong iPad.

## Lưu ý

- Micro chỉ chạy khi app đang mở trên màn hình. Khi đang nghe, app giữ màn hình sáng; nếu chuyển sang app khác,
  iPad tạm dừng micro — quay lại app, bấm ON để nghe tiếp.
- Thư viện Learning và chứng chỉ lưu trong bộ nhớ của app trên iPad (không nằm trong file Backup, giống bản máy tính).
- Bản máy tính đọc thẳng thư mục học; trên iPad hãy chọn lại thư mục/các file mỗi lần đồng bộ — file đã có sẽ tự bỏ qua.
