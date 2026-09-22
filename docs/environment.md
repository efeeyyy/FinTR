# Environment report

İlk sistem ölçümü: 2026-09-22 13:37–13:40 (Europe/Istanbul)

Kurulum ve smoke test: 2026-09-22 14:00–14:18 (Europe/Istanbul)

Repository: `C:\Users\Efe\Desktop\fintr-llm`

## Kurulum öncesi sistem

| Alan | Gözlenen değer | Yöntem/not |
|---|---|---|
| İşletim sistemi | Registry: `Windows 10 Pro`, DisplayVersion `25H2`, build `26200.9457`, x64 | Ürün adı ile build etiketi arasında olası Windows registry adlandırma tutarsızlığı var; değerler yorumlanmadan kaydedildi. |
| GPU | NVIDIA GeForce RTX 4070 Laptop GPU | `nvidia-smi --query-gpu` |
| VRAM | 8188 MiB (~8 GiB) | `nvidia-smi` |
| Compute capability | 8.9 (Ada) | `nvidia-smi --query-gpu=compute_cap` |
| NVIDIA driver | 592.82, WDDM | `nvidia-smi` |
| Driver'ın bildirdiği azami CUDA | 13.1 | Bu değer kurulu CUDA Toolkit sürümü değildir. |
| CUDA Toolkit | Standart dizinde yok; `nvcc` PATH üzerinde yok | Kaynaktan CUDA derlemesi şu anda hazır değil. |
| Python (PATH) | `python`, `python3` ve `py` bulunamadı | Kurulum öncesi durumda kullanıcı tarafından yönetilen Python yoktu. |
| Codex gömülü Python | CPython 3.12.14 x64 | Araç runtime'ı; proje ortamı olarak kullanılmamalı. |
| Git | 2.53.0.windows.3 | Codex runtime içindeki Git |
| Disk | `C:` NTFS, 952.89 GiB toplam, 545.29 GiB boş | `.NET DriveInfo`; ölçüm anlık değerdir. |
| Fiziksel bellek | 31.31 GiB toplam, 18.99 GiB kullanılabilir | Ölçüm anlık değerdir. |

### `nvidia-smi` özeti

```text
NVIDIA-SMI 592.82
Driver Version: 592.82
CUDA Version: 13.1
GPU: NVIDIA GeForce RTX 4070 Laptop GPU
Memory: 0 MiB / 8188 MiB (ölçüm anında)
Compute Capability: 8.9
Driver model: WDDM
```

## Kurulum sonrası doğrulanan geliştirme ortamı

| Alan | Gerçek test sonucu |
|---|---|
| Python dağıtımı | Resmî Python Install Manager ile CPython 3.12.10 x64 |
| Sistem Python executable | `C:\Users\Efe\AppData\Local\Python\pythoncore-3.12-64\python.exe` |
| Proje Python executable | `C:\Users\Efe\Desktop\fintr-llm\.venv\Scripts\python.exe` |
| Mimari | 64 bit (`AMD64`, pointer size 64) |
| pip | 26.2.1 |
| setuptools / wheel | 84.0.0 / 0.48.0 |
| PyTorch | 2.14.0+cu130 |
| PyTorch CUDA runtime | 13.0 |
| `torch.cuda.is_available()` | `True` |
| CUDA device count | 1 |
| GPU | NVIDIA GeForce RTX 4070 Laptop GPU |
| Compute capability | 8.9 |
| PyTorch toplam VRAM ölçümü | 8187.50 MiB |
| Başlangıçtaki boş VRAM | 7054.00 MiB |
| CUDA matris işlemi | `64x64 @ 64x64`, sonuç `cuda:0`, PASS |
| Bellek temizliği | Test tensor referansları bırakıldı, GC ve `empty_cache()` çalıştı, PASS |

Smoke test komutu:

```powershell
.venv\Scripts\python.exe scripts\smoke_cuda.py
```

Test internet veya model kullanmaz. PyTorch importu, NumPy kurulmadığı için
opsiyonel NumPy entegrasyonuna ilişkin bir uyarı verdi; CUDA sonucu ve process
çıkış kodu başarılıydı. Kapsamı genişletmemek için NumPy kurulmadı.

İlk matris çarpımı CUDA/cuBLAS süreç context'ini oluşturduğu için temizlikten
sonra sürücü seviyesinde 7006.00 MiB boş bellek ve PyTorch sayaçlarında 8.12 MiB
allocated / 20.00 MiB reserved kaldı. Test tensorlarının üç weak-reference'ı da
serbest bırakıldı; kalan süreç-seviyesi context belleği tensor sızıntısı olarak
değerlendirilmedi.

Python Install Manager paketi ayrıca kendi Python 3.14.6 runtime'ını getirdi.
FinTR-LLM `.venv` ortamı bununla değil, açıkça seçilen Python 3.12.10 ile
oluşturuldu.

## Seçilen kombinasyon ve gerekçe

- **Python:** CPython 3.12.x x64
- **PyTorch:** 2.14.0
- **PyTorch CUDA wheel:** CUDA 13.0 (`cu130`)
- **NVIDIA driver:** mevcut 592.82 korunabilir
- **Sistem CUDA Toolkit:** ilk aşamada kurulmamalı; yalnızca kaynak derleme ihtiyacı
  doğrulanırsa PyTorch wheel'iyle uyumlu sürüm ayrıca kurulmalı

Gerekçe:

1. PyTorch 2.14 resmi destek matrisinde Python 3.10–3.15 ile CUDA 12.6, 13.0
   ve 13.2 listeleniyor; varsayılan wheel CUDA 13.0.
2. NVIDIA'nın resmi uyumluluk tablosu CUDA 13.x için en az 580 sürücüsü
   gerektiğini belirtiyor. Mevcut 592.82 sürücüsü bu eşiği karşılıyor.
3. CUDA 13.2 wheel seçilmedi; `nvidia-smi` mevcut sürücünün CUDA 13.1'e kadar
   destek bildirdiğini gösteriyor. `cu130` bu makine için daha doğrudan eşleşme.
4. Hugging Face Transformers'ın güncel kararlı dokümantasyonu Python 3.10+ ve
   PyTorch 2.5+ üzerinde test edildiğini belirtiyor. Python 3.12, yeni ama daha az
   ekosistem riski taşıyan muhafazakâr bir hedef olarak seçildi.
5. Hugging Face bitsandbytes dokümantasyonu Windows x86-64 için CUDA 13.0–13.2
   wheel'lerini ve `sm89` hedefini listeliyor; bu GPU QLoRA için teorik olarak
   desteklenen sınıfta. Gerçek uyumluluk daha sonra küçük bir smoke test ile
   doğrulanmalı.

Çalıştırılan resmî PyTorch kurulum komutu:

```powershell
.venv\Scripts\python.exe -m pip install torch==2.14.0 --index-url https://download.pytorch.org/whl/cu130
```

## Resmî kaynaklar

- [PyTorch 2.14 release blog ve varsayılan CUDA 13.0 wheel](https://pytorch.org/blog/pytorch-2-14-release-blog/)
- [PyTorch release compatibility matrix](https://github.com/pytorch/pytorch/blob/main/RELEASE.md)
- [Python Install Manager 26.3](https://www.python.org/downloads/release/pymanager-263/)
- [Python 3.12.13 release notu: 3.12.10 son binary-installer sürümü](https://www.python.org/downloads/release/python-31213/)
- [Hugging Face Transformers installation](https://huggingface.co/docs/transformers/main/en/installation)
- [Hugging Face bitsandbytes installation](https://huggingface.co/docs/bitsandbytes/installation)
- [NVIDIA CUDA minor-version compatibility](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html)

Kaynaklar 2026-09-22 tarihinde kontrol edildi.

## Riskler ve açık noktalar

- 8 GiB VRAM, QLoRA yapılabilirliğini model boyutu, sequence length, batch size,
  gradient checkpointing ve optimizer durumlarına çok duyarlı kılar; model
  seçmeden önce bellek bütçesi çıkarılmalıdır.
- Native Windows üzerinde bitsandbytes wheel desteği görünse de seçilecek tam
  Transformers/PEFT/TRL/bitsandbytes sürüm seti bir kilit dosyası ve minimal CUDA
  smoke testiyle doğrulanmadan sabitlenmemelidir.
- Proje komutları aktivasyona güvenmeden `.venv\Scripts\python.exe` üzerinden
  çalıştırılmalıdır; sistem varsayılanı Python Install Manager ile 3.14.6'dır.
- Yerel CUDA Toolkit yoktur. Hazır wheel'ler için zorunlu değildir; custom CUDA
  extension derlenecekse ayrıca gerekir.
- İşletim sistemi registry ürün adı (`Windows 10 Pro`) ile `25H2`/build bilgisi
  tutarsız görünüyor. ML stack açısından kritik değil, ancak destek vakalarında
  tam `winver` çıktısı teyit edilmelidir.
- WDDM ve laptop güç/termal sınırları uzun eğitimlerde throughput ve kararlılığı
  etkileyebilir.
