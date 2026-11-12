// Version of this iPad build. Every build needs a new BUILD number (old builds stay online, like TestFlight).
// APP_VERSION follows the desktop app whose features this build matches.
export const APP_VERSION = "1.9.20";
export const BUILD = 24;
export const CHANNEL = "beta";            // "beta" (testers only) or "stable"
export const NOTES = [
  "Nghe được giọng ở xa (người nói không cầm micro), như bản Windows 1.9.20:",
  "Giọng nhỏ được nâng tiếng trước khi nhận chữ trực tiếp (giọng bình thường giữ nguyên); câu không ra chữ trực tiếp vẫn được Whisper nghe lại nếu rõ ràng to hơn tiếng ồn phòng; giữ chữ của Whisper trừ khi rõ ràng là bịa (cụm quen thuộc, nhiều chữ hơn mức nói được). Windows đo: giọng nhỏ 40 lần trước mất cả 14/14 câu, giờ 18% lỗi chữ; giọng bình thường và phòng ồn không đổi; tiếng ồn phòng không tạo dòng.",
  "Khoảng ngừng tính theo tiếng ồn của phòng, nên giọng xa không bị cắt giữa câu; đoạn ngắn dưới 1,8 giây được nối với câu sau trừ khi có khoảng ngừng thật (0,6 giây).",
  "Feedback: mỗi trang khảo sát chỉ một bảng duyệt; trang tải lại thì app điền lại câu trả lời đã duyệt; Claude không soạn được thì các câu chấm điểm vẫn được chọn sẵn mức tích cực nhất.",
  "Từ bản 23: dấu trang ltfeedback cho Feedback; bản 22: Feedback khảo sát, Claude đọc tài liệu, tự lưu phiên, dấu trang bản 3.",
];
