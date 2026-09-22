# FinTR-LLM

FinTR-LLM, Türkiye/Borsa İstanbul piyasa mikroyapısı verilerini Türkçe açıklamak
ve analiz etmek üzere geliştirilen bir araştırma projesidir. Hedef; derinlik,
aracı kurum dağılımı (AKD), takas/saklama, kurum ve fiyat-hacim verilerini katı
şemalarla normalize etmek, açık formüllerle özellikler hesaplamak ve bunları
yerel bir LLM'e kanıt sınırları belirgin bir bağlam olarak sunmaktır.

Proje yatırım tavsiyesi, hedef fiyat veya doğrudan al/sat kararı üretmez.

## Mevcut aşama

- Windows üzerinde saf PyTorch + CUDA ve bitsandbytes doğrulandı.
- Quantized Qwen base-model inference baseline'ı kaydedildi.
- Sanitize piyasa fixture'ları, stdlib veri modelleri, katı parser'lar ve
  deterministik feature engine eklendi.
- Authentication'dan ayrıştırılmış, kısa ömürlü WebSocket collector ve replay
  transport eklendi; sürekli veri toplama henüz yoktur.
- Training ve RAG henüz yoktur.

## Dizinler

- `configs/`: ilerideki veri, inference ve evaluation yapılandırmaları
- `data/`: Git dışında tutulan raw, interim, processed ve eval veri alanları
- `docs/`: ortam, inference baseline, mimari ve feature belgeleri
- `eval/`: evaluation vakaları ve sürümlü rubric'ler
- `experiments/`: tekrar üretilebilir deney kayıtları
- `scripts/`: smoke test ve yardımcı komutlar
- `src/fintr/`: veri, feature ve inference kaynak kodu
- `tests/`: sanitize fixture'lar ve otomatik testler

## Başlangıç belgeleri

- Ürün davranışı: [`PROJECT_SPEC.md`](PROJECT_SPEC.md)
- Mimari: [`docs/architecture.md`](docs/architecture.md)
- Feature formülleri: [`docs/features.md`](docs/features.md)
- Context örneği: [`docs/context_example_v0.md`](docs/context_example_v0.md)
- Collector: [`docs/collector.md`](docs/collector.md)
- Ortam raporu: [`docs/environment.md`](docs/environment.md)
- Inference baseline: [`docs/inference_baseline.md`](docs/inference_baseline.md)
- Aktif rubric: [`eval/rubrics/market_microstructure_v0.md`](eval/rubrics/market_microstructure_v0.md)

## Test

Aktivasyona bağlı kalmadan repository kökünden:

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Fixture'larda gerçek token, cookie, session veya kimlik bilgisi bulunmaz.
