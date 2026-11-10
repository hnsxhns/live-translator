// Single-file launcher for LiveTranslator.
// The app (zip) is appended to the end of this exe. On first run it is extracted once to
// %LOCALAPPDATA%\LiveTranslator\v<version>; later runs start the app from there right away.
// Trailer layout at the very end of the file: [zip bytes][int64 zip length]["LTPAYLD1"].
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Text;
using System.Threading;
using System.Windows.Forms;

static class Launcher
{
    const string Version = "__VERSION__";
    const string Magic = "LTPAYLD1";

    [STAThread]
    static void Main(string[] args)
    {
        string name = "LiveTranslator_v" + Version;
        string root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                   "LiveTranslator", "v" + Version);
        string exe = Path.Combine(root, name, name + ".exe");
        string done = Path.Combine(root, ".complete");
        string argLine = string.Join(" ", Array.ConvertAll(args, a => "\"" + a + "\""));

        if (File.Exists(done) && File.Exists(exe))
        {
            Start(exe, argLine);
            return;
        }

        Application.EnableVisualStyles();
        var form = new Form
        {
            Text = "Live Translator v" + Version,
            Width = 460, Height = 150, FormBorderStyle = FormBorderStyle.FixedDialog,
            StartPosition = FormStartPosition.CenterScreen, MaximizeBox = false, MinimizeBox = false
        };
        var label = new Label
        {
            Text = "Đang chuẩn bị ứng dụng cho lần chạy đầu tiên (khoảng 20–40 giây)...\nCác lần sau sẽ mở ngay.",
            Left = 16, Top = 14, Width = 420, Height = 40, Font = new Font("Segoe UI", 9.5f)
        };
        var bar = new ProgressBar { Left = 16, Top = 62, Width = 412, Height = 18, Style = ProgressBarStyle.Marquee };
        form.Controls.Add(label);
        form.Controls.Add(bar);

        Exception error = null;
        form.Shown += (s, e) => new Thread(() =>
        {
            try { Extract(root, done); }
            catch (Exception ex) { error = ex; }
            form.BeginInvoke((Action)(() => form.Close()));
        }) { IsBackground = true }.Start();
        Application.Run(form);

        if (error != null || !File.Exists(exe))
        {
            MessageBox.Show("Không giải nén được ứng dụng:\n" + (error != null ? error.Message : "thiếu file " + exe),
                            "Live Translator", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }
        Start(exe, argLine);
    }

    static void Start(string exe, string argLine)
    {
        Process.Start(new ProcessStartInfo(exe, argLine)
        {
            WorkingDirectory = Path.GetDirectoryName(exe), UseShellExecute = false
        });
    }

    static void Extract(string root, string done)
    {
        string self = Process.GetCurrentProcess().MainModule.FileName;
        string tmpZip = Path.Combine(Path.GetTempPath(), "LiveTranslator_" + Version + "_" + Guid.NewGuid().ToString("N") + ".zip");
        string staging = root + ".partial";
        try
        {
            using (var fs = new FileStream(self, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                var tail = new byte[16];
                fs.Seek(-16, SeekOrigin.End);
                fs.Read(tail, 0, 16);
                if (Encoding.ASCII.GetString(tail, 8, 8) != Magic)
                    throw new Exception("File bị hỏng hoặc thiếu dữ liệu ứng dụng. Hãy tải lại file .exe.");
                long len = BitConverter.ToInt64(tail, 0);
                fs.Seek(-16 - len, SeekOrigin.End);
                using (var outp = File.Create(tmpZip))
                {
                    var buf = new byte[1 << 20];
                    long left = len;
                    while (left > 0)
                    {
                        int n = fs.Read(buf, 0, (int)Math.Min(buf.Length, left));
                        if (n <= 0) throw new Exception("Đọc dữ liệu ứng dụng bị gián đoạn.");
                        outp.Write(buf, 0, n);
                        left -= n;
                    }
                }
            }
            if (Directory.Exists(staging)) Directory.Delete(staging, true);
            ZipFile.ExtractToDirectory(tmpZip, staging);
            if (Directory.Exists(root)) Directory.Delete(root, true);
            Directory.Move(staging, root);
            File.WriteAllText(done, DateTime.Now.ToString("o"));
        }
        finally
        {
            try { File.Delete(tmpZip); } catch { }
        }
    }
}
