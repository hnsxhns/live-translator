// Version of this iPad build. Every build needs a new BUILD number (old builds stay online, like TestFlight).
// APP_VERSION follows the desktop app whose features this build matches.
export const APP_VERSION = "1.9.2";
export const BUILD = 13;
export const CHANNEL = "beta";            // "beta" (testers only) or "stable"
export const NOTES = [
  "Sửa lỗi micro không đưa tiếng vào trên một số iPad (cả Safari và Chrome):",
  "Khi micro chỉ cho im lặng, app tự thử lần lượt 4 cách thu âm (kể cả cài đặt micro mặc định của iPad và bộ âm thanh cùng tần số với micro), rồi nhớ cách chạy được trên máy này cho các lần sau.",
  "App bắt đầu nghe ngay khi Vosk sẵn sàng, không còn chờ Whisper: Whisper nạp không xong (ví dụ trên GPU của iPadOS 26) từng làm app đứng mãi ở \"Đang chuẩn bị\".",
  "Whisper không trả lời trong 15 giây thì câu đó dùng chữ của Vosk; chậm hai lần thì tắt Whisper — câu chữ và bản dịch không còn bị kẹt.",
  "Hướng dẫn cấp quyền micro ghi đúng trình duyệt đang dùng (Safari, Chrome...). 🩺 Kiểm tra micro thử thêm cách thu âm mới và tự lưu cách chạy được.",
  "Từ bản 12: đủ tính năng Windows 1.9.1 (restore từng phần, hộp [cần điền], dòng \"đang làm việc\", sửa lỗi micro khi bật/tắt nhanh, gõ tiếng Việt).",
];
