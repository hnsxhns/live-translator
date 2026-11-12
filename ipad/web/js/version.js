// Version of this iPad build. Every build needs a new BUILD number (old builds stay online, like TestFlight).
// APP_VERSION follows the desktop app whose features this build matches.
export const APP_VERSION = "1.9.17";
export const BUILD = 23;
export const CHANNEL = "beta";            // "beta" (testers only) or "stable"
export const NOTES = [
  "📝 Feedback có dấu trang riêng ltfeedback: cài và thử ngay trong mục Feedback (nút 📝 Cài dấu trang ltfeedback → Copy mã → 🧪 Thử với khảo sát mẫu). Chạy ltfeedback ở trang khảo sát là app đọc câu hỏi luôn (không phải chọn giữa khảo sát và tài liệu). Dấu trang ltsource vẫn dùng được cho khảo sát.",
  "Đủ các tính năng mới của bản Windows 1.9.17 phù hợp với iPad:",
  "📝 Feedback (Learning): chọn ảnh / ảnh màn hình / slide PDF có mã QR của khảo sát (hoặc dán link) → mở khảo sát → chạy dấu trang ltsource ở đó: Claude soạn câu trả lời tích cực, cụ thể theo tài liệu và bản ghi buổi học; bạn duyệt / sửa từng câu; app điền vào form. Bạn tự bấm Next / Submit — app không bao giờ tự gửi.",
  "Hỏi Claude: Claude tự đọc toàn văn tài liệu trong thư viện và toàn bộ bản ghi buổi học (tìm theo từ) khi câu hỏi cần. Tiết kiệm credit: bối cảnh cố định gửi trước và được cache, tài liệu chỉ gửi một lần, câu hỏi về tài liệu Learning không kèm bản ghi buổi học, bản dịch chỉ gửi phần cuối.",
  "Ý chính dùng Sonnet và làm mới mỗi 60 giây (rẻ hơn nhiều). Dòng chưa dịch được ghi rõ lý do (ví dụ hết credit, mất mạng).",
  "Phiên đang mở được tự lưu mỗi phút và mỗi khi rời app, rồi tự mở lại khi vào app (Safari hay tự tải lại tab). Nút 🆕 Phiên mới: lưu phiên hiện tại vào Backup rồi bắt đầu phiên trống.",
  "Dấu trang ltsource bản 3 (cần Copy mã và dán đè một lần): chỉ lấy tài liệu của tab đang mở, nhận mã file Salesforce trong nút tải, không quét trang đăng nhập; thanh công cụ thu nhỏ (—) và kéo đi được; đọc và điền khảo sát.",
];
