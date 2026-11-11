// Version of this iPad build. Every build needs a new BUILD number (old builds stay online, like TestFlight).
// APP_VERSION follows the desktop app whose features this build matches.
export const APP_VERSION = "1.9.0";
export const BUILD = 11;
export const CHANNEL = "beta";            // "beta" (testers only) or "stable"
export const NOTES = [
  "🩺 Kiểm tra micro (nút trên thanh trên cùng): đo từng bước từ micro của iPad đến chữ tiếng Anh — quyền micro, bộ âm thanh, có tiếng vào không, Vosk/Whisper có ra chữ không — rồi chỉ ra bước bị lỗi; bấm \"Copy kết quả\" để gửi.",
  "Đủ tính năng như bản Windows 1.9.0 (bản mới nhất):",
  "Bài tập dùng đúng prompt của sách: app tự điền các chỗ trống — lựa chọn của bạn (ví dụ bàn bạn ngồi, chọn một lần, có xem trước dữ liệu), kết quả gọn của các bước trước, thông tin bạn nhập một lần — không phải copy/dán.",
  "Mỗi bước Claude trả lời đầy đủ prompt của sách rồi lưu một \"kết quả gọn\" để tự điền vào prompt các bước sau; file PDF lời giải ghi cả prompt đã dùng.",
  "Kế hoạch bài tập cũ (chưa có prompt của sách) được tự lập lại lần đầu mở.",
  "Tên file tiếng Việt từ app Tệp/Mac được chuẩn hoá (khớp đúng tên file, nhận đúng mẫu nhật ký).",
  "Từ bản 10: micro tự mở lại khi iPad treo âm thanh hoặc chỉ thu được im lặng; micro tự tắt khi rời màn hình và tự bật lại khi quay lại; thanh mức âm thanh cạnh nút ON.",
];
