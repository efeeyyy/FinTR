# FinTR-LLM

FinTR-LLM, Türkiye ve Borsa İstanbul bağlamındaki finansal dokümanları Türkçe
olarak açıklamak ve analiz etmek üzere geliştirilen bir araştırma projesidir.
Hedef; KAP açıklamaları, finansal tablolar, faaliyet raporları ve dipnotlar için
kanıta dayalı, eksik veriyi açıkça belirten bir dil modeli iş akışı kurmaktır.

Proje yatırım tavsiyesi üretmez ve doğrudan al/sat kararı vermez.

## Mevcut aşama

Bu repository yalnızca başlangıç iskeletini ve davranış/evaluation
spesifikasyonlarını içerir. Model ağırlığı indirilmemiş, veri toplanmamış ve
training başlatılmamıştır.

## Dizinler

- `configs/`: veri, training, inference ve evaluation yapılandırmaları
- `data/`: raw, interim, processed ve ayrı tutulan eval veri alanları
- `docs/`: ortam ve mimari belgeleri
- `eval/`: evaluation vakaları ve rubric'ler
- `experiments/`: tekrar üretilebilir deney kayıtları
- `scripts/`: açık giriş/çıkışlı yardımcı komutlar
- `src/fintr/`: uygulama kaynak kodu
- `tests/`: otomatik testler

## Başlangıç belgeleri

- Ürün davranışı: [`PROJECT_SPEC.md`](PROJECT_SPEC.md)
- Ortam raporu: [`docs/environment.md`](docs/environment.md)
- Mimari taslak: [`docs/architecture.md`](docs/architecture.md)
- İlk rubric: [`eval/rubrics/financial_document_analysis_v0.md`](eval/rubrics/financial_document_analysis_v0.md)

## Kurulum durumu

Henüz runtime bağımlılıkları tanımlanmamıştır. `pyproject.toml` yalnızca paket
iskeletini ve hafif geliştirme araçları için isteğe bağlı grubu tanımlar. Ortam
kararı ve önerilen sürümler için `docs/environment.md` dosyasına bakın.

