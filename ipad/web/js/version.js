// Version of this iPad build. Every build needs a new BUILD number (old builds stay online, like TestFlight).
// APP_VERSION follows the desktop app whose features this build matches.
export const APP_VERSION = "1.9.5";
export const BUILD = 15;
export const CHANNEL = "beta";            // "beta" (testers only) or "stable"
export const NOTES = [
  "Nghe tiếng Anh chính xác hơn, như bản Windows 1.9.5:",
  "Whisper được gợi ý trước các từ của khóa học (viết tắt, thuật ngữ viết hoa lặp lại trong tài liệu Learning) và ~150 ký tự vừa nghe, nên nhận đúng tên riêng và thuật ngữ hơn.",
  "Bỏ các câu Whisper tự bịa khi im lặng (\"thanks for watching\", \"please subscribe\", \"subtitles by\"...) và khi chỉ nghe được vài từ mà Whisper viết ra câu khác.",
  "Khi câu chốt khác câu nháp (dù chỉ một từ), bản dịch được làm lại và thay ngay vào dòng đó, không nháy \"…\".",
  "Kết nối tới Claude được mở sẵn khi đăng nhập và khi nghe lại sau hơn 2 phút, nên câu dịch đầu tiên không chậm hơn các câu sau.",
  "Từ bản 14: mở file Learning ngay trong app, nút ⤢ Sửa cho ô prompt, cạnh kéo giữa khung trả lời và ô prompt.",
];
