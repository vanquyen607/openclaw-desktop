# OpenClaw Desktop

Ứng dụng desktop native cho Windows, bọc quanh **OpenClaw CLI** và **Gateway** bằng [pywebview](https://pywebview.flowrl.com/) (Edge WebView2).

Gồm hai cửa sổ:

| Cửa sổ | Nội dung |
| --- | --- |
| **Control UI** | Giao diện web sẵn có của OpenClaw (chat, agents, devices, plugins, models…) chạy trong WebView2 |
| **Toolkit** | Quản trị đầy đủ không cần terminal: Gateway, Agents, Orchestration, Providers/Models, Config, Doctor, Backup, Logs, CLI runner |

## Vì sao có dự án này

- **Không cần mở terminal** để start/stop gateway, backup, update hay sửa config.
- **Không dùng Chrome** — tránh việc extension proxy làm hỏng Control UI.
- **Hết lỗi "phải nhập token mỗi lần mở"**:
  - WebView2 dùng profile bền (`private_mode=False` + `storage_path`) nên device credential được giữ lại;
  - mỗi lần khởi động vẫn lấy pairing URL một-lần mới;
  - nếu trang vẫn rơi vào màn hình nhập token, app tự phát hiện và tự sửa bằng pairing URL mới.
- Mọi lệnh CLI chạy ẩn (`CREATE_NO_WINDOW`), không bật cửa sổ console.

## Yêu cầu

- Windows 10/11 (dùng `edgechromium`, Explorer, `ctypes`)
- Python **3.10+**
- [OpenClaw CLI](https://openclaw.ai) đã cài và có trong `PATH` (`openclaw --version`)
- WebView2 Runtime (có sẵn trên Windows 11; Windows 10 cài thêm từ Microsoft)
- Tuỳ chọn: `omniroute` trong `PATH` nếu dùng provider `omniroute` (app tự khởi động daemon ở `127.0.0.1:20128`)

## Cài đặt

```bat
python -m venv "%USERPROFILE%\.venv"
"%USERPROFILE%\.venv\Scripts\python.exe" -m pip install -U pip
"%USERPROFILE%\.venv\Scripts\python.exe" -m pip install -r openclaw-gui\requirements.txt
```

## Chạy

Nhấp đúp `openclaw-gui\OpenClaw Desktop.bat`, hoặc:

```bat
"%USERPROFILE%\.venv\Scripts\pythonw.exe" openclaw-gui\app.py
```

Dùng `python.exe` thay vì `pythonw.exe` nếu muốn thấy console khi debug.

## Phím tắt & menu

- `Ctrl+T` — mở Toolkit (pywebview menu không hỗ trợ accelerator thật, nên phím tắt được gắn bằng JS)
- Menu **Control**: Toolkit, Pair lại (token mới), Reload, Start/Restart Gateway
- Menu **System**: mở thư mục logs, mở file config
- Đóng cửa sổ Toolkit = ẩn xuống nền; đóng cửa sổ Control UI = thoát app

## Cấu trúc

```
openclaw-gui/
├── app.py               # Cửa sổ Control UI, menu, JS bridge, auto-repair đăng nhập
├── gw.py                # Gateway helpers, đọc/ghi config, logs, job engine
├── toolkit.py           # Cửa sổ Toolkit (API + toàn bộ HTML/CSS/JS)
├── requirements.txt     # pywebview
└── OpenClaw Desktop.bat # Launcher dùng venv trong %USERPROFILE%\.venv
```

## Đường dẫn dữ liệu

| Mục | Đường dẫn |
| --- | --- |
| Config | `%USERPROFILE%\.openclaw\openclaw.json` |
| Profile WebView2 | `%USERPROFILE%\.openclaw\desktop-profile` |
| Backups | `%USERPROFILE%\.openclaw\backups` |
| Workspace agents | `%USERPROFILE%\.openclaw\workspace` |
| Logs | `%TEMP%\openclaw` |

## Ghi chú

- Ghi config trong Toolkit được validate qua `openclaw config validate`; nếu schema sai, file cũ được khôi phục tự động.
- `agents delete` có fallback xử lý bug Windows của OpenClaw (file id vượt `Number.MAX_SAFE_INTEGER`).
- App không lưu token/API key trong mã nguồn; mọi credential nằm trong profile WebView2 và config của OpenClaw.
