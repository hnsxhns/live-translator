// Version of this iPad build. Every build needs a new BUILD number (old builds stay online, like TestFlight).
// APP_VERSION follows the desktop app whose features this build matches.
export const APP_VERSION = "1.9.1";
export const BUILD = 12;
export const CHANNEL = "beta";            // "beta" (testers only) or "stable"
export const NOTES = [
  "Đủ tính năng như bản Windows 1.9.1 (bản mới nhất):",
  "Restore nhanh và từng phần: các dòng hiện dần khi đang khôi phục, app vẫn dùng được; trong lúc khôi phục, ON/Backup/Restore chờ cho xong.",
  "Sửa lỗi: dòng không có bản dịch làm restore dừng giữa chừng, khiến lời nói mới dùng lại số dòng cũ và có vẻ không được dịch. Dòng chưa dịch giờ hiện \"—\".",
  "Câu trả lời JSON của Claude có nhiều hơn một khối (lỗi \"Extra data\") được đọc từng khối, lấy khối đúng cuối cùng.",
  "Bài tập: các chỗ [cần điền: ...] mở một hộp hỏi ngắn từng câu, rồi tự điền vào prompt.",
  "Dòng \"đang làm việc\" (vòng xoay) trên thanh trên cùng khi Claude trả lời, khôi phục, đồng bộ thư mục, đọc/chuẩn bị bài tập, điền nhật ký.",
  "Sửa lỗi tìm ra khi kiểm tra: tắt rồi bật ON nhanh (hoặc bật ON ngay sau Restore) làm micro im lặng dù nút vẫn là ON; chuyển màn hình qua lại rất nhanh có thể để lại hai micro cùng thu (chữ bị lặp/rối) hoặc micro không bật lại.",
  "Sửa lỗi: Whisper ghi \"[BLANK_AUDIO]\" thay cho câu tiếng Anh khi đoạn âm thanh im lặng.",
  "Sửa lỗi: gõ tiếng Việt (Telex/VNI) trong ô Hỏi Claude, phím Enter để chốt chữ có thể gửi luôn câu đang gõ dở.",
  "Giao diện gọn và nhẹ hơn: thanh Claude/Learning hai bên sáng và nhẹ, thanh công cụ dọc màn hình nằm trên một dòng (cả iPad mini), đường chia Anh | Việt không còn bị nhoè khi bản ghi dài.",
  "Từ bản 11: 🩺 Kiểm tra micro; bài tập dùng đúng prompt của sách (Windows 1.9.0). Từ bản 10: micro tự mở lại, tự tắt/bật khi rời/quay lại màn hình.",
];
