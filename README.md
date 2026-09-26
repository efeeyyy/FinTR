# FinTR-LLM

FinTR-LLM, Türkiye/Borsa İstanbul piyasa mikroyapısı verilerini Türkçe açıklamak
ve analiz etmek üzere geliştirilen bir araştırma projesidir. Hedef; derinlik,
aracı kurum dağılımı (AKD), takas/saklama, kurum ve fiyat-hacim verilerini katı
şemalarla normalize etmek, açık formüllerle özellikler hesaplamak ve bunları
kaynak ve provenance sınırları belirgin bir bağlamda incelemektir.

Proje yatırım tavsiyesi, hedef fiyat veya doğrudan al/sat kararı üretmez.

## Mevcut hedef

- Güncel piyasa verisini toplamak, kaynak/zaman/istek provenance'ını doğrulamak
  ve sınırlı bir analiz bağlamı hazırlamak.
- Hazırlanan kanıtları Efe ile birlikte daha güçlü yönetici modelde incelemek;
  bu repository doğrudan bir model API'si çağırmaz.
- Yerel model inference'ı ve model eğitimi proje kapsamında değildir.
- Sanitize piyasa fixture'ları, katı parser'lar, provenance modelleri ve
  deterministik feature engine analiz hazırlık katmanını sağlar.
- WebSocket collector kısa ömürlüdür; sürekli veri toplama henüz yoktur.

## Dizinler

- `configs/`: ilerideki veri, inference ve evaluation yapılandırmaları
- `data/`: Git dışında tutulan raw, interim, processed ve eval veri alanları
- `docs/`: mimari, veri toplama, context ve feature belgeleri
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
- Bellek içi canlı analiz bağlamı: [`docs/live_context.md`](docs/live_context.md)
- Çevrimiçi test öncesi çevrimdışı kontrol: [`docs/offline_preflight.md`](docs/offline_preflight.md)
- Değerlendirme protokolü: [`docs/evaluation_protocol.md`](docs/evaluation_protocol.md)
- Python ortamı: [`docs/environment.md`](docs/environment.md)
- Aktif rubric: [`eval/rubrics/market_microstructure_v0.md`](eval/rubrics/market_microstructure_v0.md)

## Test

Aktivasyona bağlı kalmadan repository kökünden:

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Fixture'larda gerçek token, cookie, session veya kimlik bilgisi bulunmaz.

## Kaydedilmiş dönem paketini analiz etme

Ağ ve model çağrısı olmadan, deterministik formüllerle
([`docs/features.md`](docs/features.md#dönem-paketi-kontrolleri-period_features_v2)):

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\analyze_period.py data\interim\period_analysis\AKFIS\2026-09-08_2026-09-25.json
```

Bütün kayıtlar için tek komut ve kayıtlar arası indeks: `--all` (raporları
kaydetmek için `--save`; mevcut raporların üzerine yazılmaz).

## Değerlendirme vakalarını doğrulama

Gold vakalar insan annotation'ı gerektirir; repo yalnız formatı ve sızıntı
korumasını sağlar ([`docs/evaluation_protocol.md`](docs/evaluation_protocol.md)).
Geliştirmede kullanılmış semboller (`src/fintr/evaluation/dev_registry.json`)
tarihten bağımsız olarak eval vakası olamaz.

```powershell
.\.venv\Scripts\python.exe scripts\validate_eval_manifest.py eval\cases\manifest.example.json
```
