# Environment report

Kurulum: 2026-09-22 (Europe/Istanbul). GPU/CUDA/PyTorch kısımları yerel model
çalıştırma hedefi rafa kaldırıldığı için 2026-09-26'da bu belgeden çıkarıldı.

Repository: `C:\Users\Efe\Desktop\fintr-llm`

## Python ortamı

| Alan | Değer |
|---|---|
| Python dağıtımı | Resmî Python Install Manager ile CPython 3.12.10 x64 |
| Sistem Python executable | `C:\Users\Efe\AppData\Local\Python\pythoncore-3.12-64\python.exe` |
| Proje Python executable | `C:\Users\Efe\Desktop\fintr-llm\.venv\Scripts\python.exe` |
| Mimari | 64 bit (`AMD64`) |
| Çalışma bağımlılığı | `websockets` (`pyproject.toml`) |

Python Install Manager paketi ayrıca kendi Python 3.14.6 runtime'ını getirdi.
FinTR-LLM `.venv` ortamı bununla değil, açıkça seçilen Python 3.12.10 ile
oluşturuldu. Proje komutları aktivasyona güvenmeden `.venv\Scripts\python.exe`
üzerinden çalıştırılmalıdır.

Proje yalnız standart kütüphane ve `websockets` kullanır; model, GPU veya CUDA
bağımlılığı yoktur.
