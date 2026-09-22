# Quantized local inference baseline

Test zamanı: 2026-09-22 (Europe/Istanbul)

## Kapsam

Bu test yalnızca yerel, tek-prompt inference doğrulamasıdır. Training, PEFT/LoRA,
TRL, dataset, scraping veya RAG çalıştırılmadı.

## Model

- Model ID: `Qwen/Qwen3-4B-Instruct-2507`
- Exact revision: `cdbee75f17c01a7cc42f958dc650907174af0554`
- Model kartı: <https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507>
- Lisans: Apache-2.0
- Repository payload: 8,060,917,568 byte (7,687.49 MiB)
- Ölçülen dış cache kullanımı: 8,060,920,045 byte (7,687.49 MiB, 7.507 GiB)
- Cache mantıksal yolu: `C:\Users\Efe\AppData\Local\fintr-llm\huggingface`
- Cache fiziksel/AppX yolu:
  `C:\Users\Efe\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\fintr-llm\huggingface`

Model ağırlıkları repository dışında tutuldu. Windows symlink desteği kapalı olduğu
için Hugging Face cache sistemi degraded-mode uyarısı verdi; bu testte ölçülen
fiziksel kullanım repository payload'ına yakındı.

## Dependency sürümleri

| Paket | Sürüm |
|---|---:|
| Python | 3.12.10 x64 |
| torch | 2.14.0+cu130 |
| numpy | 2.5.3 |
| transformers | 5.17.0 |
| accelerate | 1.15.0 |
| bitsandbytes | 0.50.2 |
| safetensors | 0.8.0 |
| huggingface_hub | 1.32.0 |

`pip check`: `No broken requirements found.`

`datasets`, `peft` ve `trl` kurulu değildir.

## bitsandbytes ve BF16 doğrulaması

- Native backend: `CudaBNBNativeLibrary`
- CUDA ile derlenmiş backend: `True`
- GPU: `NVIDIA GeForce RTX 4070 Laptop GPU`
- Compute capability: `8.9`
- PyTorch BF16 desteği: `True`
- Küçük BF16 CUDA matmul: PASS
- NF4 quantize/dequantize CUDA round trip: PASS
- Quantized storage: `torch.uint8`, device `cuda:0`
- Nested/double quantization state: `True`

bitsandbytes, Triton bulunmadığı için opsiyonel FLOP counting uyarısı verdi. Native
CUDA NF4 operasyonu ve model quantization yolu başarılı olduğundan Triton bu test
için kurulmadı.

## Quantization ve yerleşim

```json
{
  "load_in_4bit": true,
  "bnb_4bit_quant_type": "nf4",
  "bnb_4bit_use_double_quant": true,
  "bnb_4bit_compute_dtype": "torch.bfloat16",
  "device_map": {"": 0},
  "cpu_offload": false
}
```

- `model.is_loaded_in_4bit`: `True`
- Model parameter device seti: yalnızca `cuda:0`
- BF16 fallback gerekmedi.
- BF16/FP16 tam-ağırlık GPU yüklemesi yapılmadı.

## Ölçümler

Ölçümler exact cached revision'ın ikinci, eksiksiz yakalanan çalışmasındandır.
İndirme ilk çalışmada tamamlandı; ikinci çalışmada `cache_delta_bytes=0` idi.
Model load süresi download süresini içermez.

| Ölçüm | Sonuç |
|---|---:|
| Model load süresi | 7.471 s |
| Load sonrası allocated VRAM | 2,554.41 MiB |
| Load sonrası reserved VRAM | 2,594.00 MiB |
| Load + generation peak VRAM | 2,617.72 MiB |
| Prompt token sayısı | 83 |
| Generated token sayısı | 300 |
| Generation süresi | 17.074 s |
| Yaklaşık hız | 17.57 token/s |
| Batch size | 1 |
| `max_new_tokens` | 300 |

OOM oluşmadı ve CPU offload kullanılmadı.

## Test promptu

```text
Bir şirketin net kârı yıllık bazda %45 artmış ancak işletme faaliyetlerinden nakit akışı negatife dönmüş. Yalnızca bu bilgilere dayanarak finansal durumu değerlendir ve daha sağlıklı yorum yapabilmek için hangi ek verilere ihtiyaç duyduğunu belirt.
```

Chat template yalnızca tek `user` mesajı ve generation prompt içerdi; system prompt
ve FinTR davranış talimatı eklenmedi. Üretim `do_sample=False` ile deterministik
yapıldı.

## Raw response

Aşağıdaki çıktı değiştirilmeden korunmuştur. Model 300-token sınırına ulaştığı
için yanıt son kelimenin ortasında kesilmiştir.

```text
Verilen bilgiye dayanarak bir şirketin **net kârı yıllık bazda %45 artmış** olmasına rağmen **işletme faaliyetlerinden nakit akışı negatife dönmüş** olmasının bir finansal durum değerlendirmesinde çok önemli bir sinyal olduğunu görebiliriz. Bu durum, **net kâr ile nakit akış arasındaki fark** açısından çok dikkat çekici olup, şirketin gerçek ekonomik performansının net kâr gibi bir kâr göstergesinden daha zayıf olabileceğini gösterir.

---

### 🔍 Durumun Yorumu (Sadece Verilen Bilgilere Dayanarak)

1. **Net Kâr %45 Artış:**
   - Bu, mali tabloların genel olarak "kâr" kavramını yansıtır. Net kâr, gelirlerden maliyetlerin ve diğer giderlerin (özellikle finansal giderler, amortisman, kâr payı, vergi gibi) çıkarılmasıyla elde edilir.
   - %45 artışı, şirketin genel verimliliğinin arttığını, satışları arttırmış veya maliyetlerin düşmesini gösterir.

2. **İşletme Faali
```

## Karşılaşılan sorunlar

1. İlk download oturumundaki progress çıktısı sonuç bloğunun bir kısmını terminal
   limitinde kırptı. Snapshot tekrar indirilmeden cache üzerinden ikinci ölçüm
   alındı.
2. Raw yanıttaki `🔍` karakteri Windows CP1254 stdout üzerinde
   `UnicodeEncodeError` oluşturdu. Script stdout/stderr'i UTF-8 olarak ayarlayacak
   şekilde düzeltildi; model çıktısı değiştirilmedi.
3. Model 300-token sınırında bitmedi ve çıktı kesildi. Baseline bütünlüğü için
   prompt veya raw yanıt değiştirilmedi.
4. `model.hf_device_map` bu Transformers sürümünde `null` döndü; CPU offload
   olmadığını tüm model parametrelerinin gerçek device setinin yalnızca `cuda:0`
   olmasıyla doğruladık.
