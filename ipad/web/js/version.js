// Version of this iPad build. Every build needs a new BUILD number (old builds stay online, like TestFlight).
// APP_VERSION follows the desktop app whose features this build matches.
export const APP_VERSION = "1.9.8";
export const BUILD = 18;
export const CHANNEL = "beta";            // "beta" (testers only) or "stable"
export const NOTES = [
  "Nhãn Giáo viên hiện sớm hơn: người nói chính (ít nhất một nửa tổng thời gian nói) thành Giáo viên sau 3 phút, không phải chờ đủ 10 phút như trước (Windows 1.9.8).",
  "Đủ các cải tiến phù hợp với iPad của bản Windows 1.9.8:",
  "Lọc âm cho Whisper: bỏ tiếng ù/rì (quạt, điều hoà, gõ bàn) và tăng dần tiếng người nói nhỏ hoặc ở xa (tối đa 10 lần, không khuếch đại tiếng ồn) — Windows đo: người nói xa 9,9% → 5,8% lỗi từ.",
  "Ý chính viết sạch như ghi chú học: không còn \"(chưa rõ do lỗi nhận dạng)\", tự sửa từ nghe nhầm theo ngữ cảnh và thuật ngữ khóa học, bỏ chuyện ngoài lề; chỉ gửi phần ý chính gần đây nên nhanh và rẻ hơn khi buổi học dài.",
  "🌐 Source trong Learning: dán link trang web chứa tài liệu một lần; mỗi lần bấm, app quét trang, tải tài liệu mới, bỏ file trùng nội dung, upload cho Claude và phân loại. (Chỉ chạy với trang cho phép ứng dụng web đọc; trang chặn thì app báo rõ cách khác.)",
  "Danh sách thuật ngữ khóa học được làm mới ngay khi đăng nhập và sau mỗi lần đồng bộ thư viện.",
  "Từ bản 16: chạm một lần để dán key khi quay lại từ trang Claude. Từ bản 15: nghe tiếng Anh chính xác hơn như Windows 1.9.5.",
];
